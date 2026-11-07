// Axis positions (contract section 13): polarization angle, XYZ, Picomotor steps
// (pushed live by the satellite); the optical stage on the STM computer and the
// Nanonis optics group (read on demand).
//
// Satellite device positions come from the hub's cached axis_state / device_state
// (GET /api/satellites) and never touch the device. The stage local to the STM
// computer has no push: the "Read position" button runs the read-only skill
// OpticalStageGetPos, a real hardware read, so it happens only on click and is
// never polled. (/api/optics/position on the optics page is the old direct path and
// new code does not use it.)

import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Badge, Card, EmptyNote } from "@/components/ui";
import { SkillRunButton } from "@/components/skills/SkillRunButton";
import { useSatellites } from "@/api/satellites";
import { axisReadouts, DEVICE_KIND_LABEL } from "@/lib/satellites";
import { fmtValue } from "@/lib/live/scale";
import { tr } from "@/lib/i18n";

const AXIS_KINDS = new Set(["rotation_stage", "xyz_stage", "openloop_stepper"]);

export function AxisPanel() {
  const sats = useSatellites();
  const optics = useQuery({
    queryKey: ["optics", "devices"],
    queryFn: async () => {
      const r = await api.GET("/api/optics/devices");
      const status = r.response.status;
      // With an empty error body, error is "" (falsy) and data is undefined: check both.
      if (r.error || !r.data) throw new Error(tr(`光学台设备清单读取失败（HTTP ${status}）`, `Failed to read optical stage device list (HTTP ${status})`));
      return r.data;
    },
    refetchInterval: 15_000,
  });

  const satAxes = sats.data?.deployed
    ? sats.data.satellites.flatMap((s) =>
        s.devices.filter((d) => AXIS_KINDS.has(d.kind)).map((d) => ({ sat: s, dev: d })),
      )
    : [];
  const localDevices = (optics.data?.devices ?? []).filter((d) => d.enabled);

  return (
    <Card>
      <h3 className="mb-3 text-sm font-semibold text-mast-text">{tr("轴位置", "Axis positions")}</h3>

      <div className="space-y-2">
        {satAxes.length === 0 && (
          <EmptyNote label={sats.data?.deployed ? tr("卫星上没有运动类设备", "No motion devices on the satellite") : tr("卫星模块未部署", "Satellite module not deployed")} />
        )}
        {satAxes.map(({ sat, dev }) => (
          <div key={`${sat.id}/${dev.id}`} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs">
            <span className="w-36 shrink-0">
              <span className="font-mono text-mast-text">
                {sat.id}/{dev.id}
              </span>
              <span className="block text-[10.5px] text-mast-faint">{DEVICE_KIND_LABEL[dev.kind] ?? dev.kind}</span>
            </span>
            {axisReadouts(dev).map((r) => (
              <span key={r.label} title={r.note} className="whitespace-nowrap">
                <span className="text-mast-faint">{r.label} </span>
                <span className="font-mono text-sm tabular-nums text-mast-text">{r.value}</span>
                {r.moving && <span className="ml-1 text-mast-info">{tr("动", "moving")}</span>}
              </span>
            ))}
            {sat.state !== "connected" && sat.state !== "degraded" ? (
              <Badge tone="WARN">{tr("最后已知", "Last known")}</Badge>
            ) : (
              dev.stale && <Badge tone="WARN">{tr("陈旧", "Stale")}</Badge>
            )}
            {dev.operatorHold && <Badge tone="WARN">{tr("人工接管", "Manual override")}</Badge>}
          </div>
        ))}
      </div>

      <div className="mt-4 border-t border-mast-border pt-3">
        <div className="mb-2 text-xs font-semibold text-mast-text">{tr("STM 电脑上的光学台", "Optical stage on the STM computer")}</div>
        {localDevices.length === 0 ? (
          <p className="text-xs text-mast-faint">
            {optics.isError ? tr("光学台设备清单读取失败", "Failed to read optical stage device list") : tr("没有启用的本机光学台（config/optical_instruments.json）", "No local optical stage enabled (config/optical_instruments.json)")}
          </p>
        ) : (
          <div className="space-y-1.5">
            {localDevices.flatMap((d) =>
              (d.axes ?? []).map((a) => (
                <div key={`${d.id}/${a.name}`} className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="w-44 shrink-0 font-mono text-mast-muted">
                    {d.id} · {a.name}
                  </span>
                  <SkillRunButton
                    skill="OpticalStageGetPos"
                    label={tr("读位置", "Read position")}
                    variant="ghost"
                    immediate
                    params={() => ({ device_id: d.id, axis: a.name })}
                    renderResult={(r) => {
                      const pos = r.data?.position;
                      return `${typeof pos === "number" ? fmtValue(pos) : "—"} ${String(r.data?.unit ?? a.unit ?? "")}`;
                    }}
                  />
                </div>
              )),
            )}
          </div>
        )}
      </div>

      <div className="mt-4 border-t border-mast-border pt-3">
        <div className="mb-2 text-xs font-semibold text-mast-text">{tr("Nanonis 光学组", "Nanonis optics group")}</div>
        <SkillRunButton skill="ListNanonisMotorGroups" label={tr("列出光学组", "List optics groups")} variant="ghost" immediate params={() => ({})} />
        <p className="mt-1 text-[11px] text-mast-faint">
          {tr("反射镜 / Beam Deflection / 透镜等粗动组。STM 粗动组不在这里动（契约 §9）。", "Coarse groups such as mirrors / Beam Deflection / lenses. The STM coarse group is not moved here (contract section 9).")}
        </p>
      </div>
    </Card>
  );
}
