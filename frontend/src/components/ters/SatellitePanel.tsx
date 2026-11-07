// Satellite and device status card (contract section 13): per-satellite link, RTT,
// clock offset and boot_id; per-device state, manual override and stale markers; and
// port-ownership warnings (section 10.5).
//
// Data comes only from the hub (GET /api/satellites, /api/port-guard): it reads state
// held in hub memory and never touches the device. Every readout carries a staleness
// flag: after a satellite restart or disconnect, positions and settings cannot be
// trusted (section 5.3); they are still shown, but marked, and must not be read as
// current values.

import clsx from "clsx";
import { Badge, Card, EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { Button } from "@/components/controls";
import { usePortGuard, useRefreshPortGuard, useSatellites } from "@/api/satellites";
import { fmtAgo, useNow } from "@/hooks/useLive";
import { tr } from "@/lib/i18n";
import {
  DEVICE_KIND_LABEL,
  DEVICE_STATE_LABEL,
  LIVENESS_LABEL,
  attentionItems,
  axisReadouts,
  clockOffsetLevel,
  deviceStateTone,
  estopLeftovers,
  livenessTone,
  spectrometerReadouts,
  type DeviceInfo,
  type PortFinding,
  type PortGuardSnapshot,
  type SatelliteInfo,
} from "@/lib/satellites";

function fmtMs(v: number | null, digits = 1): string {
  return v === null ? "—" : `${v.toFixed(digits)} ms`;
}

export function SatellitePanel() {
  const sats = useSatellites();
  const guard = usePortGuard();
  const now = useNow(1000);

  const deployed = sats.data?.deployed ? sats.data : null;
  const guardSnap = guard.data?.deployed ? guard.data : null;
  const attention = deployed ? attentionItems(deployed, guardSnap) : [];

  return (
    <Card>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-mast-text">{tr("卫星与设备", "Satellites and devices")}</h3>
        {deployed?.hub && (
          <span className="text-xs text-mast-faint">
            {tr("枢纽", "Hub ")}{deployed.hub.started ? tr("运行中", "running") : tr("未启动", "not started")}
            {deployed.hub.configured !== null && tr(` · 配置 ${deployed.hub.configured} 颗`, ` · ${deployed.hub.configured} configured`)}
            {deployed.hub.connected !== null && tr(` · 在线 ${deployed.hub.connected} 颗`, ` · ${deployed.hub.connected} online`)}
          </span>
        )}
      </div>

      {sats.isPending && <Spinner label={tr("读取卫星状态…", "Reading satellite state…")} />}
      {sats.isError && <ErrorNote error={sats.error} label={tr("卫星状态读取失败", "Failed to read satellite state")} />}
      {sats.data && !sats.data.deployed && (
        <EmptyNote label={tr("后端还没有卫星模块（GET /api/satellites 返回 404）—— 升级 MAST 后这里显示光谱电脑（卫星）等节点。", "The backend has no satellite module yet (GET /api/satellites returned 404). After upgrading MAST, nodes such as the spectrometer computer (satellite) appear here.")} />
      )}
      {deployed?.degraded && (
        <div className="mb-2 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-xs text-mast-warn">
          {tr("卫星枢纽状态读取出错（degraded）：下面可能不全。", "Satellite hub state could not be read fully (degraded): the list below may be incomplete.")}
        </div>
      )}

      {attention.length > 0 && (
        <ul className="mb-3 space-y-1 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-xs text-mast-warn">
          {attention.map((a) => (
            <li key={a}>⚠ {a}</li>
          ))}
        </ul>
      )}

      {deployed && deployed.satellites.length === 0 && (
        <EmptyNote label={tr("没有配置卫星（<数据根>/config/satellites.json），也没有卫星连进来。", "No satellites configured (<data root>/config/satellites.json) and none connected.")} />
      )}

      <div className="space-y-4">
        {deployed?.satellites.map((s) => <SatelliteBlock key={s.id} s={s} now={now} />)}
      </div>

      <PortGuardBlock
        loading={guard.isPending}
        error={guard.error}
        notDeployed={!!guard.data && !guard.data.deployed}
        snap={guardSnap}
      />
    </Card>
  );
}

function SatelliteBlock({ s, now }: { s: SatelliteInfo; now: number }) {
  const lvl = clockOffsetLevel(s.clockOffsetMs);
  return (
    <div className="rounded-mast-ctl border border-mast-border bg-mast-panel-2 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-semibold text-mast-text">{s.id}</span>
        <Badge tone={livenessTone(s.state)}>{LIVENESS_LABEL[s.state]}</Badge>
        {s.note && <span className="text-xs text-mast-muted">{s.note}</span>}
        {!s.configured && <Badge tone="WARN">{tr("未在配置里", "Not in config")}</Badge>}
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
        <Stat label="RTT" value={fmtMs(s.rttMs)} />
        <Stat
          label={tr("时钟偏差", "Clock offset")}
          value={fmtMs(s.clockOffsetMs)}
          tone={lvl === "bad" ? "danger" : lvl === "warn" ? "warn" : undefined}
          title={tr("θ = 卫星时钟 − 枢纽时钟；≥ 500 ms 会让 HLC 抛异常", "θ = satellite clock − hub clock; ≥ 500 ms makes the HLC raise")}
        />
        <Stat
          label={s.state === "connected" || s.state === "degraded" ? tr("已连接", "Connected") : tr("最后见到", "Last seen")}
          value={
            s.connectedSinceMs !== null
              ? fmtAgo(s.connectedSinceMs, now).replace(/ (前|ago)$/, "")
              : fmtAgo(s.lastSeenMs, now)
          }
        />
        <Stat label="boot_id" value={s.bootId ? s.bootId.slice(0, 10) : "—"} title={s.bootId ?? undefined} mono />
        <Stat label={tr("重启次数", "Restarts")} value={s.restarts === null ? "—" : String(s.restarts)} title={tr("枢纽看到 boot_id 变化的次数（含蓝屏）", "Number of boot_id changes seen by the hub (including crashes)")} />
        <Stat label={tr("在途命令", "Pending commands")} value={s.pendingCommands === null ? "—" : String(s.pendingCommands)} />
      </dl>
      {(s.sw || s.host) && (
        <div className="mt-1 font-mono text-[11px] text-mast-faint">
          {[s.sw, s.host, s.peer].filter(Boolean).join(" · ")}
        </div>
      )}
      {s.lastEstop && <EstopLine s={s} now={now} />}

      {s.devices.length > 0 && (
        <div className="mt-3 divide-y divide-mast-border border-t border-mast-border">
          {s.devices.map((d) => (
            <DeviceRow key={d.id} d={d} now={now} linkUp={s.state === "connected" || s.state === "degraded"} />
          ))}
        </div>
      )}
    </div>
  );
}

const ACTIVE_LABEL = (a: boolean | null) =>
  a === true
    ? tr("现在还在动", "still moving")
    : a === false
      ? tr("现在没在动", "not moving")
      : tr("现在动没动说不准", "movement state unknown");

/**
 * Last emergency stop and the satellite's acknowledgement. Devices that did not stop
 * are listed one by one with the satellite's own message: the case this line guards
 * against is an e-stop reported as executed while a stage is still moving. Devices
 * that were not moving are recorded but not highlighted (a satellite's xyz
 * stage has no remote stop, so it always appears here).
 */
function EstopLine({ s, now }: { s: SatelliteInfo; now: number }) {
  const e = s.lastEstop!;
  const left = estopLeftovers(s);
  return (
    <div className="mt-1 space-y-0.5 text-[11px]">
      <div className="text-mast-warn">
        {tr("上次急停：", "Last emergency stop: ")}{e.reason || tr("（无原因）", "(no reason)")}
        {e.sentMs !== null && ` · ${fmtAgo(e.sentMs, now)}`}
      </div>
      {!e.ack ? (
        <div className="text-mast-warn">{tr("卫星还没有回执", "No acknowledgement from the satellite yet")}</div>
      ) : (
        <div className="text-mast-muted">
          {tr("回执：已停 ", "Acknowledged: stopped ")}{e.ack.stopped.length ? e.ack.stopped.join(tr("、", ", ")) : "—"}
          {e.ack.active && e.ack.active.length > 0 && tr(`（其中当时在动的：${e.ack.active.join("、")}）`, ` (of which moving at the time: ${e.ack.active.join(", ")})`)}
        </div>
      )}
      {left.map((x) => (
        <div key={x.device} className={x.active === false ? "text-mast-muted" : "font-medium text-mast-danger"}>
          {tr("没停下来 ", "Did not stop: ")}{x.device}{tr("：", ": ")}{x.message || tr("卫星没给原因", "satellite gave no reason")}{tr("（", " (")}{ACTIVE_LABEL(x.active)}{tr("）", ")")}
        </div>
      ))}
    </div>
  );
}

/**
 * One device is two rows: name plus status badges, then readouts. The right panel is
 * often narrow and table columns would wrap a label one character per line, so this
 * does not use a table.
 *
 * When the link is down (lost or never connected), readouts are the last known
 * values, not current ones: they are marked "Last known" and dimmed. When the link
 * is up but the hub set stale (delayed heartbeat; no new device_state yet after a
 * satellite restart) they are marked "Stale" and dimmed. The hub also sets stale on
 * disconnect (newer hubs; older ones only on restart); only one badge is shown and
 * the disconnected state takes precedence.
 */
function DeviceRow({ d, now, linkUp }: { d: DeviceInfo; now: number; linkUp: boolean }) {
  const readouts = d.kind === "spectrometer" ? spectrometerReadouts(d) : axisReadouts(d);
  const hw = [d.driver, d.model, d.serial && `SN ${d.serial}`].filter(Boolean).join(" · ");
  const notLive = d.stale || !linkUp;
  return (
    <div className="py-2 text-xs">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-mono font-medium text-mast-text">{d.id}</span>
        <span className="text-mast-faint">{DEVICE_KIND_LABEL[d.kind] ?? d.kind}</span>
        {d.role && <span className="font-mono text-[10.5px] text-mast-faint">role {d.role}</span>}
        <span className="flex flex-wrap gap-1 whitespace-nowrap">
          <Badge tone={deviceStateTone(d.state)}>{DEVICE_STATE_LABEL[d.state] ?? d.state}</Badge>
          {d.operatorHold && d.state !== "operator_hold" && <Badge tone="WARN">{tr("人工接管", "Manual override")}</Badge>}
          {d.sim && <Badge tone="INFO">{tr("模拟", "Simulated")}</Badge>}
          {!linkUp ? <Badge tone="WARN">{tr("最后已知", "Last known")}</Badge> : d.stale && <Badge tone="WARN">{tr("陈旧", "Stale")}</Badge>}
        </span>
        {d.statusMs !== null && <span className="text-[10.5px] text-mast-faint">{tr("状态", "state")} {fmtAgo(d.statusMs, now)}</span>}
      </div>
      {hw && <div className="mt-0.5 font-mono text-[11px] text-mast-faint">{hw}</div>}
      {readouts.length > 0 && (
        <div className={clsx("mt-1 flex flex-wrap gap-x-3 gap-y-0.5", notLive && "opacity-60")}>
          {readouts.map((r) => (
            <span key={r.label} title={r.note} className="whitespace-nowrap">
              <span className="text-mast-faint">{r.label} </span>
              <span className="font-mono tabular-nums text-mast-text">{r.value}</span>
              {r.moving && <span className="ml-1 text-mast-info">· {tr("动", "moving")}</span>}
              {r.note && <span className="ml-1 text-mast-faint">{tr("（", "(")}{r.note}{tr("）", ")")}</span>}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
  title,
  mono,
}: {
  label: string;
  value: string;
  tone?: "warn" | "danger";
  title?: string;
  mono?: boolean;
}) {
  return (
    <div title={title}>
      <dt className="text-mast-faint">{label}</dt>
      <dd
        className={clsx(
          "tabular-nums",
          mono && "font-mono",
          tone === "danger" ? "text-mast-danger" : tone === "warn" ? "text-mast-warn" : "text-mast-text",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

function PortGuardBlock({
  loading,
  error,
  notDeployed,
  snap,
}: {
  loading: boolean;
  error: unknown;
  notDeployed: boolean;
  snap: PortGuardSnapshot | null;
}) {
  const refresh = useRefreshPortGuard();
  const now = useNow(5000);
  return (
    <div className="mt-4 border-t border-mast-border pt-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-semibold text-mast-text">{tr("端口归属（MAST 为枢纽）", "Port ownership (MAST is the hub)")}</span>
        <span className="flex items-center gap-2 text-xs text-mast-faint">
          {snap?.checkedMs != null && <span>{tr("检查于", "Checked")} {fmtAgo(snap.checkedMs, now)}</span>}
          <Button variant="ghost" loading={refresh.isPending} onClick={() => refresh.mutate()}>
            {tr("重新检查", "Recheck")}
          </Button>
        </span>
      </div>
      {loading && <Spinner label={tr("读取端口归属…", "Reading port ownership…")} />}
      {!!error && <ErrorNote error={error} label={tr("端口归属读取失败", "Failed to read port ownership")} />}
      {notDeployed && <p className="text-xs text-mast-faint">{tr("后端还没有端口归属检查（/api/port-guard 404）。", "The backend has no port-ownership check yet (/api/port-guard 404).")}</p>}
      {snap && (
        <div className="space-y-1 text-xs">
          {snap.conflicts.length === 0 && snap.warnings.length === 0 && snap.complete && (
            <p className="text-mast-auto">{tr("没有发现别的程序占着 Nanonis 端口或光学设备。", "No other program was found holding the Nanonis port or optical devices.")}</p>
          )}
          {!snap.complete && (
            <p className="text-mast-warn">{tr("有几项检查没跑成（见下面的原因）—— 没列出冲突不等于没有冲突。", "Some checks did not run (see reasons below); no listed conflict does not mean no conflict.")}</p>
          )}
          {snap.conflicts.map((f) => (
            <Finding key={`c-${f.resource}-${f.pid}`} f={f} tone="DANGEROUS" />
          ))}
          {snap.warnings.map((f) => (
            <Finding key={`w-${f.resource}-${f.pid}`} f={f} tone="WARN" />
          ))}
          {!snap.enforce && <p className="text-mast-faint">{tr("已配置为只报告（enforce=false），冲突不会阻止组合技能开始。", "Configured as report-only (enforce=false); conflicts do not block composite skills from starting.")}</p>}
          {snap.errors.map((e) => (
            <p key={e} className="text-mast-warn">{tr("检查没跑全：", "Check incomplete: ")}{e}</p>
          ))}
          <p className="text-mast-faint">{tr("MAST 不会替你关别的程序 —— 要用 MAST 控制的设备，请先在那个程序里断开。", "MAST will not close other programs for you. To control a device with MAST, disconnect it in that program first.")}</p>
        </div>
      )}
    </div>
  );
}

function Finding({ f, tone }: { f: PortFinding; tone: string }) {
  return (
    <div className="flex flex-wrap items-baseline gap-2">
      <Badge tone={tone}>{tone === "DANGEROUS" ? tr("冲突", "Conflict") : tr("注意", "Notice")}</Badge>
      <span className="font-mono text-mast-text">{f.resource}</span>
      <span className="text-mast-muted">
        {tr("被 ", "held by ")}{f.process}
        {f.pid !== null && ` (pid ${f.pid})`}{tr(" 占用", "")}
      </span>
      {f.detail && <span className="text-mast-faint">{f.detail}</span>}
    </div>
  );
}
