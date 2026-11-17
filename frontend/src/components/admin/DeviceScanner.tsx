import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Badge, Card, DegradedNote, EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { Button, Checkbox, Modal, TextField, useToast } from "@/components/controls";
import { tr } from "@/lib/i18n";

/** Scan device ports: scan → confirm → adopt.
 *
 *  Probing the serial ports and swapping whatever was found straight into the live
 *  monitor leaves no chance to say "that's not right". This flow keeps the two
 *  halves apart:
 *
 *    GET  /api/environment/discover        identify only, persists NOTHING
 *    POST /api/environment/sensors/adopt   write the entries the user ticked
 *
 *  Adoption is what makes a port stick — the entry lands in
 *  environment_sensors.json, so every later boot rebuilds it before autodetect
 *  runs and the instrument is found without another scan.
 *
 *  InstrumentStateCard is the read-only readout of everything MAST can ask the
 *  instrument (inputs, sensor types, and the heater state). Every field behind
 *  it comes from a query command. The one command this panel can send is the magnet
 *  supply's STOP (MagnetControlBar), which only ever freezes a ramp. */

type Suggested = Record<string, unknown>;

const fmt = (v: number | null | undefined, digits = 3, unit = ""): string =>
  v == null ? "—" : `${v.toFixed(digits)}${unit ? " " + unit : ""}`;

/** One input's reading status: three states, never two.
 *
 *  `faults` is tri-state on the wire: `[]` = the instrument answered `RDGST?`
 *  and no fault bit is set, non-empty = it answered and the reading is NOT
 *  trustworthy, `null` = the status query never answered, so nothing is known
 *  about this reading's validity.
 *
 *  `(c.faults ?? []).length > 0` would read `null` as clean and label an unbacked
 *  reading "OK". Prefer the backend's own `health` verdict so the three-way rule
 *  has one definition; the local derivation is only for a payload that predates
 *  the field, and it defaults to unknown rather than to clean. */
function ReadingStatus({ faults, health }: {
  faults?: string[] | null;
  health?: string | null;
}) {
  const verdict = health ?? (faults == null ? "unknown" : faults.length ? "faulty" : "clean");
  if (verdict === "faulty") {
    return <Badge tone="DANGEROUS">{(faults ?? []).join(tr("、", ", ")) || tr("读数不可信", "Reading not trustworthy")}</Badge>;
  }
  if (verdict === "clean") {
    return <span className="text-mast-muted">{tr("正常", "OK")}</span>;
  }
  return (
    <Badge tone="WARN">
      <span
        title={tr(
          "RDGST? 状态查询没有回复 —— 这个温度没有仪器的故障位背书，不等于正常",
          "The RDGST? status query got no reply, so this temperature has no fault-bit confirmation from the instrument. That does not mean it is OK.",
        )}
      >
        {tr("状态位未读到", "Status bits unread")}
      </span>
    </Badge>
  );
}

const yesNo = (v: boolean | null | undefined): string =>
  v == null ? "—" : v ? tr("是", "Yes") : tr("否", "No");

/** Small two-column key/value grid used by the magnet and level-meter readouts. */
function KV({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-md border border-mast-border px-3 py-2 text-sm">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <span className="text-mast-muted">{k}</span>
          <span className="font-mono">{v}</span>
        </div>
      ))}
    </div>
  );
}

/** Lake Shore 625 magnet supply: read-only state. Every value is a query reply;
 *  `null` means the instrument did not answer that query, shown as "—". */
export function MagnetDetails({ m }: { m: any }) {
  if (!m) return <EmptyNote label={tr("未读到磁体电源状态", "Magnet supply state not read")} />;
  const fu = m.field_unit || "T";
  const errors: string[] = [
    ...(m.hardware_errors ?? []),
    ...(m.operational_errors ?? []),
    ...(m.psh_errors ?? []),
  ];
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {m.quench_detected ? <Badge tone="DANGEROUS">{tr("检测到失超", "Quench detected")}</Badge> : null}
        {m.crowbar_discharge ? <Badge tone="DANGEROUS">{tr("crowbar 放电中", "Crowbar discharge")}</Badge> : null}
        {errors.length > 0 && !m.quench_detected && !m.crowbar_discharge ? (
          <Badge tone="WARN">{errors.join(tr("、", ", "))}</Badge>
        ) : null}
        {m.any_error === false && <Badge tone="AUTO">{tr("无错误", "No errors")}</Badge>}
        {m.ramping ? <Badge tone="WARN">{tr("扫场中", "Ramping")}</Badge> : m.ramping === false ? (
          <span className="text-xs text-mast-muted">{tr("未扫场", "Not ramping")}</span>
        ) : null}
        {m.energized ? <Badge tone="WARN">{tr("磁体有电流", "Magnet energized")}</Badge> : null}
      </div>
      <KV rows={[
        [tr("计算磁场", "Computed field"), fmt(m.field, 4, fu)],
        [tr("输出电流", "Output current"), fmt(m.current_a, 4, "A")],
        [tr("输出电压 / 远端电压", "Output / remote voltage"), `${fmt(m.voltage_v, 4, "V")} / ${fmt(m.remote_voltage_v, 4, "V")}`],
        [tr("设定电流 / 设定磁场", "Setpoint current / field"), `${fmt(m.setpoint_current_a, 4, "A")} / ${fmt(m.setpoint_field, 4, fu)}`],
        [tr("场常数", "Field constant"), m.field_constant == null ? "—" : `${m.field_constant} ${fu}/A`],
        [tr("扫场速率", "Ramp rate"), fmt(m.ramp_rate_a_s, 4, "A/s")],
        [tr("分段扫场", "Ramp segments"),
          m.ramp_segments_enabled === false ? tr("关", "Off")
            : (m.ramp_segments ?? []).length === 0 ? "—"
            : (m.ramp_segments ?? [])
                .map((s: any) => `<${fmt(s.upper_current_a, 1, "A")}: ${fmt(s.rate_a_s, 4, "A/s")}`)
                .join(" · ")],
        [tr("限值（电流/电压/速率）", "Limits (current/voltage/rate)"),
          `${fmt(m.limit_current_a, 3, "A")} / ${fmt(m.limit_voltage_v, 3, "V")} / ${fmt(m.limit_rate_a_s, 4, "A/s")}`],
        [tr("限值对应最大场", "Max field at limit"), fmt(m.max_field, 3, fu)],
        [tr("持续开关", "Persistent switch"),
          m.psh_enabled === false ? tr("未配置", "Not configured") : (m.psh_state ?? "—")],
        [tr("失超检测", "Quench detection"),
          m.quench_detection_enabled == null ? "—" : m.quench_detection_enabled
            ? `${tr("开", "On")} (${fmt(m.quench_step_limit_a_s, 3, "A/s")})` : tr("关", "Off")],
        [tr("电流编程", "Current programming"), m.external_program ?? "—"],
        [tr("面板模式", "Front-panel mode"), m.interface_mode ?? "—"],
        [tr("顺从限", "In compliance"), yesNo(m.in_compliance)],
      ]} />
    </div>
  );
}

const _minutes = (s: number | null | undefined): string =>
  s == null ? "—" : s < 90 ? tr(`${Math.round(s)} 秒`, `${Math.round(s)} s`)
    : tr(`${Math.round(s / 60)} 分钟`, `${Math.round(s / 60)} min`);

/** Field control on the Model 625 (7.2.0): may MAST change the field, the ramp MAST is
 *  running, and the operator's STOP. STOP freezes the output where it is; it does not go
 *  to zero. The button is always there: stopping is never gated. */
export function MagnetControlBar() {
  const queryClient = useQueryClient();
  const { toast, node } = useToast();
  const q = useQuery({
    queryKey: ["environment", "magnet-control"],
    // No serial I/O behind this endpoint (it reports MAST's own state), so it can poll fast.
    refetchInterval: 3000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/environment/magnet-control");
      if (error) throw error;
      return data;
    },
  });
  const stop = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/environment/magnet-control/stop", {
        body: { reason: "" },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      if (res?.sent) {
        toast(tr("已发 STOP：磁体输出冻结在当前值", "STOP sent: magnet output frozen at its present value"));
      } else {
        toast(tr(`STOP 没发出去：${res?.error || "未知原因"}。请在 625 面板上按 Stop Output`,
                 `STOP was not sent: ${res?.error || "unknown"}. Press Stop Output on the 625 panel`), "err");
      }
      queryClient.invalidateQueries({ queryKey: ["environment"] });
    },
    onError: () => toast(tr("STOP 请求失败。请在 625 面板上按 Stop Output", "The STOP request failed. Press Stop Output on the 625 panel"), "err"),
  });

  const d = q.data;
  if (!d?.present) return null;
  const s = (d.settings ?? {}) as Record<string, any>;
  const active = d.active as Record<string, any> | null | undefined;
  const last = d.last as Record<string, any> | null | undefined;
  return (
    <div className="space-y-1 rounded-md border border-mast-border px-3 py-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-mast-text">{tr("MAST 调场", "MAST field control")}</span>
        {d.can_set_field ? (
          <Badge tone="INFO">
            {tr(`已启用：上限 ${s.max_field_t} T，场常数 ${s.field_constant_t_per_a} T/A`,
                `Enabled: limit ${s.max_field_t} T, field constant ${s.field_constant_t_per_a} T/A`)}
          </Badge>
        ) : (
          <span className="text-xs text-mast-muted">
            {tr("未启用：填好最大磁场与场常数之前，MAST 不改变磁场。",
                "Not enabled: MAST does not change the field until the maximum field and the field constant are filled in. ")}
            {/* A link, not a path to read: the page where they are entered is one click away. */}
            <Link to="/settings/setup" className="text-mast-accent underline">
              {tr("去「设置 → 仪器初始化」填写", "Fill them in under Settings → Instrument Setup")}
            </Link>
          </span>
        )}
        {active ? (
          <Badge tone="WARN">
            {tr(`MAST 扫场中 → ${active.target_t} T（预计 ${_minutes(active.eta_s)}；${active.origin ?? ""}）`,
                `MAST ramping → ${active.target_t} T (about ${_minutes(active.eta_s)}; ${active.origin ?? ""})`)}
          </Badge>
        ) : d.busy ? (
          <Badge tone="WARN">{tr("MAST 正在准备调场", "MAST is preparing a field change")}</Badge>
        ) : null}
        <span className="ml-auto">
          <Button variant="danger" disabled={stop.isPending} onClick={() => stop.mutate()}>
            {tr("停止扫场（冻结，不回零）", "Stop ramp (freeze, not zero)")}
          </Button>
        </span>
      </div>
      {!active && last ? (
        <div className="text-xs text-mast-muted">
          {tr(`上一次：${last.outcome} —— ${last.detail ?? ""}`, `Last: ${last.outcome}: ${last.detail ?? ""}`)}
        </div>
      ) : null}
      {node}
    </div>
  );
}

/** Cryomagnetics LM-510: read-only state of one channel. */
export function LevelMeterDetails({ l }: { l: any }) {
  if (!l) return <EmptyNote label={tr("未读到液面计状态", "Level monitor state not read")} />;
  const flags: string[] = l.status_flags ?? [];
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {l.sensor_fault ? <Badge tone="DANGEROUS">{tr("探头故障（读数不可信）", "Sensor fault (reading not valid)")}</Badge> : null}
        {l.updates_by_itself === false && (
          <Badge tone="WARN">
            <span title={tr("仪器采样模式为 Off：液面不会自动刷新，下面是上一次测量的值。MAST 不会触发测量（测量会加热探头、蒸发液氦）。", "Sample mode is Off on the instrument: the level does not refresh by itself; below is the last measurement. MAST never starts a measurement (it heats the sensor and boils off helium).")}>
              {tr("Mode=Off：读数不会自动更新", "Mode=Off: reading does not refresh")}
            </span>
          </Badge>
        )}
        {l.menu_open ? <Badge tone="WARN">{tr("面板菜单打开中", "Front-panel menu open")}</Badge> : null}
      </div>
      <KV rows={[
        [tr("液面（上次测量）", "Level (last measurement)"), l.level == null ? "—" : `${l.level} ${l.unit ?? ""}`],
        [tr("通道 / 类型", "Channel / type"), `${l.channel ?? 1} / ${l.channel_type ?? "—"}`],
        [tr("采样模式 / 间隔", "Sample mode / interval"), `${l.sample_mode || "—"} / ${l.sample_interval || "—"}`],
        [tr("探头有效长度", "Sensor active length"), l.sensor_length || "—"],
        [tr("Boost", "Boost"), l.boost || "—"],
        [tr("报警限（低/高）", "Alarm limits (low/high)"), `${l.low_alarm || "—"} / ${l.high_alarm || "—"}`],
        [tr("补液控制", "Refill control"), l.control || "—"],
        [tr("状态位", "Status bits"), flags.length ? flags.join(tr("、", ", ")) : l.status_code == null ? "—" : tr("无", "None")],
      ]} />
    </div>
  );
}

/** One discovered instrument, with a tick + editable name per input. */
function DeviceBlock({
  device, picks, onToggle, onRename,
}: {
  device: Record<string, never> extends never ? any : any;
  picks: Record<string, { on: boolean; name: string }>;
  onToggle: (id: string, on: boolean) => void;
  onRename: (id: string, name: string) => void;
}) {
  if (!device.identified) {
    return (
      <div className="rounded-md border border-mast-border bg-mast-bg/40 px-3 py-2 text-xs text-mast-muted">
        <span className="font-mono">{device.port}</span>
        {device.description ? ` — ${device.description}` : ""} {tr("— 未识别到已知仪器", "— no known instrument recognised")}
      </div>
    );
  }
  const suggested: Suggested[] = device.suggested_sensors ?? [];
  return (
    <div className="rounded-md border border-mast-accent/40 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-mast-text">
          {device.model || tr("未知型号", "Unknown model")}
        </span>
        <span className="font-mono text-xs text-mast-muted">{device.port}</span>
        {device.firmware && <Badge tone="INFO">{tr("固件", "Firmware")} {device.firmware}</Badge>}
        {device.already_configured && <Badge tone="AUTO">{tr("已接入", "Adopted")}</Badge>}
        {/* The heater badge belongs to temperature controllers only: a magnet supply
            or a level monitor has no heater, and "Heater off" there would be invented. */}
        {(device.kind ?? "lakeshore_temp") === "lakeshore_temp" &&
          (device.any_heater_on ? (
            <Badge tone="DANGEROUS">{tr("加热器开启", "Heater on")}</Badge>
          ) : (
            <Badge tone="AUTO">{tr("加热器关闭", "Heater off")}</Badge>
          ))}
        {device.kind === "lakeshore_625" && <Badge tone="INFO">{tr("超导磁体电源 · 只读", "Magnet supply · read-only")}</Badge>}
        {device.kind === "cryomag_lm510" && <Badge tone="INFO">{tr("液面计 · 只读", "Level monitor · read-only")}</Badge>}
      </div>
      {device.idn && (
        <div className="mb-2 break-all font-mono text-[11px] text-mast-muted">{device.idn}</div>
      )}
      {device.kind === "lakeshore_625" && <div className="mb-3"><MagnetDetails m={device.magnet} /></div>}
      {device.kind === "cryomag_lm510" && <div className="mb-3"><LevelMeterDetails l={device.level_meter} /></div>}
      {device.kind === "lakeshore_other" && (
        <div className="mb-2 text-xs text-mast-muted">
          {tr("这是 Lake Shore 的仪器，但 MAST 没有这个型号的驱动，不会接入或轮询它。", "This is a Lake Shore instrument, but MAST has no driver for this model and will not connect to or poll it.")}
        </div>
      )}

      {(device.channels ?? []).length > 0 && (
        <div className="mb-3 overflow-auto rounded-md border border-mast-border">
          <table className="w-full text-sm">
            <thead className="text-mast-muted">
              <tr className="border-b border-mast-border">
                <th className="px-2 py-1 text-left">{tr("输入", "Input")}</th>
                <th className="px-2 py-1 text-left">{tr("仪器上的名字", "Name on instrument")}</th>
                <th className="px-2 py-1 text-left">{tr("当前读数", "Current reading")}</th>
                <th className="px-2 py-1 text-left">{tr("传感器类型", "Sensor type")}</th>
                <th className="px-2 py-1 text-left">{tr("状态", "Status")}</th>
              </tr>
            </thead>
            <tbody>
              {(device.channels ?? []).map((c: any) => (
                <tr key={c.channel} className="border-b border-mast-border/50">
                  <td className="px-2 py-1 font-mono">{c.channel}</td>
                  <td className="px-2 py-1">{c.label || "—"}</td>
                  <td className="px-2 py-1 font-mono">{fmt(c.kelvin, 3, "K")}</td>
                  <td className="px-2 py-1">{c.sensor_type || "—"}</td>
                  <td className="px-2 py-1">
                    <ReadingStatus faults={c.faults} health={c.health} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="mb-1 text-xs text-mast-muted">
        {tr("勾选要接入 MAST 的通道（名字取自仪器，可改）：", "Tick the channels to connect to MAST (names come from the instrument and can be edited):")}
      </div>
      <div className="space-y-2">
        {suggested.map((s) => {
          const id = String(s.id);
          const pick = picks[id] ?? { on: true, name: String(s.name ?? id) };
          return (
            <div key={id} className="flex flex-wrap items-center gap-2">
              <Checkbox
                checked={pick.on}
                onChange={(v) => onToggle(id, v)}
                label={<span className="font-mono text-xs">{String(s.channel ?? s.quantity ?? "")} →</span>}
              />
              <div className="min-w-[12rem] flex-1">
                <TextField value={pick.name} onChange={(v) => onRename(id, v)} />
              </div>
            </div>
          );
        })}
        {suggested.length === 0 && <EmptyNote label={tr("该设备没有可接入的通道", "This device has no channels to connect")} />}
      </div>
    </div>
  );
}

export function DeviceScanButton() {
  const queryClient = useQueryClient();
  const { toast, node } = useToast();
  const [open, setOpen] = useState(false);
  const [devices, setDevices] = useState<any[]>([]);
  const [picks, setPicks] = useState<Record<string, { on: boolean; name: string }>>({});

  const scan = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.GET("/api/environment/discover");
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      if (res?.degraded) {
        toast(tr("扫描未执行（内核未接入）。", "Scan not run (core not attached)."), "err");
        return;
      }
      const found = (res?.devices ?? []) as any[];
      setDevices(found);
      // Pre-tick everything identified, pre-filled with the instrument's own
      // input labels, so the user confirms rather than retypes.
      const next: Record<string, { on: boolean; name: string }> = {};
      for (const d of found) {
        for (const s of (d.suggested_sensors ?? []) as Suggested[]) {
          next[String(s.id)] = { on: true, name: String(s.name ?? s.id) };
        }
      }
      setPicks(next);
      setOpen(true);
    },
    onError: (e) => toast(`${tr("扫描失败：", "Scan failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
  });

  const adopt = useMutation({
    mutationFn: async () => {
      const chosen: Suggested[] = [];
      for (const d of devices) {
        for (const s of (d.suggested_sensors ?? []) as Suggested[]) {
          const pick = picks[String(s.id)];
          if (pick?.on) chosen.push({ ...s, name: pick.name.trim() || String(s.name ?? s.id) });
        }
      }
      const { data, error } = await api.POST("/api/environment/sensors/adopt", {
        body: { sensors: chosen } as never,
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["environment"] });
      if (!res?.ok) {
        toast(
          tr(
            `未接入任何设备${res?.rejected?.length ? "：" + res.rejected.join("；") : "。"}`,
            `No device was adopted${res?.rejected?.length ? ": " + res.rejected.join("; ") : "."}`,
          ),
          "err",
        );
        return;
      }
      setOpen(false);
      toast(tr(`已接入 ${res.adopted} 个通道，开机后自动连接。`, `${res.adopted} channel(s) adopted; they will connect automatically at start-up.`), "ok");
    },
    onError: (e) => toast(`${tr("接入失败：", "Adopt failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
  });

  const chosenCount = Object.values(picks).filter((p) => p.on).length;
  const identified = devices.filter((d) => d.identified).length;

  return (
    <>
      <Button variant="default" disabled={scan.isPending} onClick={() => scan.mutate()}>
        {scan.isPending ? tr("扫描中…", "Scanning…") : tr("扫描设备接口", "Scan device ports")}
      </Button>

      <Modal open={open} onClose={() => setOpen(false)} title={tr("扫描结果 — 请确认识别是否正确", "Scan result — confirm the identification is correct")} wide>
        <div className="space-y-3">
          {devices.length === 0 ? (
            <EmptyNote label={tr("没有找到任何串口", "No serial ports found")} />
          ) : identified === 0 ? (
            <>
              <EmptyNote label={tr("扫描到串口，但没有识别出已知仪器", "Serial ports found, but no known instrument was recognised")} />
              {devices.map((d) => (
                <DeviceBlock key={d.port} device={d} picks={picks} onToggle={() => {}} onRename={() => {}} />
              ))}
            </>
          ) : (
            devices.map((d) => (
              <DeviceBlock
                key={d.port}
                device={d}
                picks={picks}
                onToggle={(id, on) =>
                  setPicks((p) => ({ ...p, [id]: { ...(p[id] ?? { name: id }), on } }))
                }
                onRename={(id, name) =>
                  setPicks((p) => ({ ...p, [id]: { ...(p[id] ?? { on: true }), name } }))
                }
              />
            ))
          )}

          <p className="rounded-md border border-mast-border bg-mast-bg/40 px-2 py-1.5 text-xs text-mast-muted">
            {tr(
              "扫描只是读取，不会改变任何配置。点「接入 MAST」后才写入本地设置，之后每次开机会自动连接这个端口。扫描和读数只发查询命令；只有在「仪器初始化」里设好调场或控温之后，MAST 才会经过闸门改变磁场或开加热器，从不触发液面测量。",
              "Scanning only reads; it changes no configuration. Local settings are written only after you click “Connect to MAST”, and the port then connects automatically at every start-up. Scanning and readings send only query commands; MAST changes the field or turns a heater on only through its gated controls, once field or temperature control is set up under Instrument Setup, and it never starts a level measurement.",
            )}
          </p>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="primary"
              disabled={adopt.isPending || chosenCount === 0}
              onClick={() => adopt.mutate()}
            >
              {adopt.isPending ? tr("接入中…", "Connecting…") : tr(`接入 MAST（${chosenCount} 个通道）`, `Connect to MAST (${chosenCount} channel(s))`)}
            </Button>
            <Button variant="ghost" onClick={() => setOpen(false)}>{tr("取消", "Cancel")}</Button>
          </div>
        </div>
      </Modal>
      {node}
    </>
  );
}

/** Instrument settings: the full read-only readout. */
export function InstrumentStateCard() {
  const q = useQuery({
    queryKey: ["environment", "instrument-state"],
    // Goes over the serial bus (sharing the monitor's handle), so poll gently.
    refetchInterval: 15000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/environment/instrument-state");
      if (error) throw error;
      return data;
    },
  });

  const instruments = q.data?.instruments ?? [];
  // The heater badge summarises temperature controllers only; a magnet supply or a
  // level monitor in the list must not turn into a "Heater off" claim.
  const thermo = instruments.filter((i) => (i.kind || "lakeshore_temp") === "lakeshore_temp");

  return (
    <Card>
      <div className="mb-2 flex items-center gap-2">
        <span className="text-sm font-medium text-mast-text">{tr("仪器设定（只读）", "Instrument settings (read-only)")}</span>
        {thermo.some((i) => i.any_heater_on) ? (
          <Badge tone="DANGEROUS">{tr("加热器开启", "Heater on")}</Badge>
        ) : thermo.length > 0 ? (
          <Badge tone="AUTO">{tr("加热器关闭", "Heater off")}</Badge>
        ) : null}
      </div>
      {q.isPending && <Spinner />}
      {q.error && <ErrorNote error={q.error} />}
      {q.data?.degraded && <DegradedNote what={tr("仪器设定", "Instrument settings")} />}
      {q.data && !q.data.degraded && instruments.length === 0 && (
        <EmptyNote label={tr("未连接任何仪器 —— 可用「扫描设备接口」查找", "No instrument connected — use “Scan device ports” to find one")} />
      )}
      <div className="space-y-4">
        {instruments.map((inst) => (
          <div key={inst.port || inst.idn} className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm text-mast-text">{inst.model || tr("仪器", "Instrument")}</span>
              <span className="font-mono text-xs text-mast-muted">{inst.port}</span>
              {inst.firmware && <Badge tone="INFO">{tr("固件", "Firmware")} {inst.firmware}</Badge>}
              {inst.serial_number && (
                <span className="font-mono text-[11px] text-mast-muted">S/N {inst.serial_number}</span>
              )}
            </div>

            {inst.kind === "lakeshore_625" ? (
              <>
                <MagnetControlBar />
                <MagnetDetails m={inst.magnet} />
              </>
            ) : inst.kind === "cryomag_lm510" ? (
              <LevelMeterDetails l={inst.level_meter} />
            ) : (
            <>

            <div className="overflow-auto rounded-md border border-mast-border">
              <table className="w-full text-sm">
                <thead className="text-mast-muted">
                  <tr className="border-b border-mast-border">
                    <th className="px-2 py-1 text-left">{tr("输入", "Input")}</th>
                    <th className="px-2 py-1 text-left">{tr("名称", "Name")}</th>
                    <th className="px-2 py-1 text-left">{tr("温度 (K)", "Temperature (K)")}</th>
                    <th className="px-2 py-1 text-left">{tr("温度 (°C)", "Temperature (°C)")}</th>
                    <th className="px-2 py-1 text-left">{tr("原始量", "Raw value")}</th>
                    <th className="px-2 py-1 text-left">{tr("传感器", "Sensor")}</th>
                    <th className="px-2 py-1 text-left">{tr("状态", "Status")}</th>
                  </tr>
                </thead>
                <tbody>
                  {(inst.channels ?? []).map((c) => (
                    <tr key={c.channel} className="border-b border-mast-border/50">
                      <td className="px-2 py-1 font-mono">{c.channel}</td>
                      <td className="px-2 py-1">{c.label || "—"}</td>
                      <td className="px-2 py-1 font-mono">{fmt(c.kelvin)}</td>
                      <td className="px-2 py-1 font-mono">{fmt(c.celsius, 2)}</td>
                      <td className="px-2 py-1 font-mono">{fmt(c.sensor_value, 4)}</td>
                      <td className="px-2 py-1">{c.sensor_type || "—"}</td>
                      <td className="px-2 py-1">
                        <ReadingStatus faults={c.faults} health={c.health} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {(inst.outputs ?? []).length > 0 && (
              <div className="overflow-auto rounded-md border border-mast-border">
                <table className="w-full text-sm">
                  <thead className="text-mast-muted">
                    <tr className="border-b border-mast-border">
                      <th className="px-2 py-1 text-left">{tr("控制输出", "Control output")}</th>
                      <th className="px-2 py-1 text-left">{tr("加热档位", "Heater range")}</th>
                      <th className="px-2 py-1 text-left">{tr("输出", "Output")}</th>
                      <th className="px-2 py-1 text-left">{tr("设定值", "Setpoint")}</th>
                      <th className="px-2 py-1 text-left">{tr("控制模式", "Control mode")}</th>
                      <th className="px-2 py-1 text-left">{tr("控制输入", "Control input")}</th>
                      <th className="px-2 py-1 text-left">{tr("升温速率", "Ramp rate")}</th>
                      <th className="px-2 py-1 text-left">{tr("断电自恢复", "Power-up enable")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(inst.outputs ?? []).map((o) => (
                      <tr key={o.output} className="border-b border-mast-border/50">
                        <td className="px-2 py-1 font-mono">{o.output}</td>
                        <td className="px-2 py-1">
                          {o.heater_on ? (
                            <Badge tone="DANGEROUS">{o.range_label || o.range_code}</Badge>
                          ) : (
                            <span className="text-mast-muted">{o.range_label || tr("关闭", "Off")}</span>
                          )}
                        </td>
                        <td className="px-2 py-1 font-mono">{fmt(o.heater_pct, 1, "%")}</td>
                        <td className="px-2 py-1 font-mono">{fmt(o.setpoint, 2, "K")}</td>
                        <td className="px-2 py-1">{o.mode || "—"}</td>
                        <td className="px-2 py-1">{o.control_input || "—"}</td>
                        <td className="px-2 py-1 font-mono">
                          {o.ramping ? fmt(o.ramp_rate, 1, "K/min") : tr("关闭", "Off")}
                        </td>
                        <td className="px-2 py-1">{o.powerup_enabled ? tr("是", "Yes") : tr("否", "No")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            </>
            )}
          </div>
        ))}
      </div>
      {instruments.length > 0 && (
        <p className="mt-2 rounded-md border border-mast-border bg-mast-bg/40 px-2 py-1.5 text-xs text-mast-muted">
          {tr(
            "以上全部为只读查询。改变磁场、开关加热器只走调场和控温两套带闸门的控制（要先在「仪器初始化」里设好）；MAST 从不开关持续开关、不触发液面测量，也不改温控仪的控制模式、PID 和温度上限 —— 这些由操作员在仪器上完成。",
            "Everything above is a read-only query. The field and the heater change only through the gated field and temperature controls (set up under Instrument Setup first); MAST never switches the persistent switch, never starts a level measurement, and never changes the controller's control mode, PID or temperature limit; those are done by the operator at the instrument.",
          )}
        </p>
      )}
    </Card>
  );
}
