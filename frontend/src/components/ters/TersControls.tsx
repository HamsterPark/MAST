// Control buttons for the spectroscopy / TERS page. Every one is only an entry point
// to skill execution (POST /api/skills/{name}/execute): the safety gate, instrument
// token, abort and recording on the server path are all kept (contract section 13).
//
// Only spectrometer and acquisition buttons live here (skill names and parameters are
// defined in skills/builtins/optics_spectro.py; an empty device_id means "find the
// only device with that role"). Buttons for polarization, Picomotor, the XYZ stage,
// the Nanonis optics group, the optical stage on the STM computer and the alignment
// camera are in the "Manual control" section of the optics page (contract section
// 13.1); this page only links there so that there is one copy.
//
// Stop skills (StopSpectrometer / StopSatelliteDevices) need no token and still run
// after an abort, so they show no confirm dialog.

import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Card } from "@/components/ui";
import { Field, NumberField, SelectField, TextField } from "@/components/controls";
import { SkillRunButton } from "@/components/skills/SkillRunButton";
import { MANUAL_SECTION_ID } from "@/components/manual/ManualControlSection";
import { useHardwareModules } from "@/api/hardwareModules";
import { moduleEnabled, TERS_OPTICS_MODULE } from "@/lib/satellites";
import { tr } from "@/lib/i18n";

/** Number from an input: empty string = not given (optional parameter is omitted), invalid = NaN. */
function parseNum(s: string): number | null {
  const t = s.trim();
  if (!t) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : Number.NaN;
}

function Group({ title, children, note }: { title: string; children: ReactNode; note?: ReactNode }) {
  return (
    <div className="border-t border-mast-border pt-3 first:border-t-0 first:pt-0">
      <div className="mb-2 text-xs font-semibold text-mast-text">{title}</div>
      <div className="flex flex-wrap items-end gap-2">{children}</div>
      {note && <div className="mt-1.5 text-[11px] text-mast-faint">{note}</div>}
    </div>
  );
}

/**
 * One-line note shown when the ters_optics hardware module is off. Off only affects
 * the agent's tool list; the buttons still execute, so this is an explanation, not a
 * warning, and does not disable them. Nothing is shown when the module state cannot
 * be read (old backend, failed request): unknown is not the same as off. The switch
 * is under Advanced -> System / Global -> Capability switches.
 */
function ModuleHint() {
  const q = useHardwareModules();
  if (moduleEnabled(q.data, TERS_OPTICS_MODULE) !== false) return null;
  return (
    <p className="mb-3 rounded-mast-ctl border border-mast-border bg-mast-bg/40 px-3 py-2 text-xs text-mast-muted">
      {tr("硬件模块 ters_optics 未开启（", "Hardware module ters_optics is off (")}
      <Link to="/settings/admin" className="text-mast-accent hover:underline">
        {tr("设置 → 高级管理 → 系统 / 全局 → 能力开关", "Settings → Advanced → System / Global → Capability switches")}
      </Link>
      {tr("，需要管理 PIN）：界面上的按钮照常可用，只是 agent 看不到这些技能。", ", requires the admin PIN): the buttons here still work; only the agent cannot see these skills.")}
    </p>
  );
}

export const LASER_NOTE = tr(
  "激光没有任何软件联锁：「停止」只停采集、停轴、关光谱仪入口快门，照到样品上的光不会被挡住。离开或出现异常时请手动关激光 / 挡光。",
  "The laser has no software interlock: Stop only halts acquisition, stops axes and closes the spectrometer entrance shutter; light reaching the sample is not blocked. Switch off or block the laser manually when leaving or when something goes wrong.",
);

export function TersControls() {
  const [exposure, setExposure] = useState("1000");
  const [frames, setFrames] = useState("1");
  const [tag, setTag] = useState("");
  const [center, setCenter] = useState("");
  const [grating, setGrating] = useState("");
  const [port, setPort] = useState<"front" | "side">("front");
  const acqParams = (dry: boolean) => {
    const p: Record<string, unknown> = {};
    const e = parseNum(exposure);
    const f = parseNum(frames);
    if (e !== null) p.exposure_ms = e;
    if (f !== null) p.frames = f;
    if (tag.trim()) p.tag = tag.trim();
    if (dry) p.dry_run = true;
    return p;
  };
  const acqCheck = () => {
    const e = parseNum(exposure);
    const f = parseNum(frames);
    if (e !== null && (Number.isNaN(e) || e <= 0)) return tr("曝光时间要是正数（毫秒），或者留空用光谱仪当前设置", "Exposure time must be a positive number (ms), or leave empty to use the spectrometer's current setting");
    if (f !== null && (Number.isNaN(f) || f < 1 || !Number.isInteger(f))) return tr("帧数要是 ≥ 1 的整数，或者留空", "Frame count must be an integer ≥ 1, or leave empty");
    return null;
  };

  return (
    <Card>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-mast-text">{tr("控制", "Controls")}</h3>
        <span className="text-[11px] text-mast-faint">{tr("按钮只调技能（经安全闸门与仪器仲裁），不直连驱动", "Buttons only call skills (through the safety gate and instrument arbitration), never the driver directly")}</span>
      </div>
      <ModuleHint />

      <div className="space-y-3">
        <Group title={tr("停止", "Stop")} note={LASER_NOTE}>
          <SkillRunButton skill="StopSpectrometer" label={tr("停止光谱仪", "Stop spectrometer")} variant="danger" immediate params={() => ({})} />
          <SkillRunButton
            skill="StopSatelliteDevices"
            label={tr("停止全部卫星设备", "Stop all satellite devices")}
            variant="danger"
            immediate
            params={() => ({})}
          />
        </Group>

        <Group title={tr("采谱（AcquireSpectrum）", "Acquire spectrum (AcquireSpectrum)")} note={tr("不填 = 用光谱仪当前设置。修针 / 进针进行中会被拒绝；谱自动归档进当前样品，并实时推到上面的光谱图。", "Empty = use the spectrometer's current setting. Refused while tip conditioning or approach is running; the spectrum is archived into the current sample automatically and pushed live to the spectrum chart above.")}>
          <div className="w-28">
            <Field label={tr("曝光 (ms)", "Exposure (ms)")}>
              <NumberField value={exposure} onChange={setExposure} />
            </Field>
          </div>
          <div className="w-20">
            <Field label={tr("帧数", "Frames")}>
              <NumberField value={frames} onChange={setFrames} />
            </Field>
          </div>
          <div className="w-32">
            <Field label={tr("标签", "Tag")}>
              <TextField value={tag} onChange={setTag} placeholder={tr("可留空", "Optional")} />
            </Field>
          </div>
          <SkillRunButton skill="AcquireSpectrum" label={tr("试跑", "Dry run")} params={() => acqParams(true)} validate={acqCheck} />
          <SkillRunButton
            skill="AcquireSpectrum"
            label={tr("采一条谱", "Acquire a spectrum")}
            variant="primary"
            params={() => acqParams(false)}
            validate={acqCheck}
            describe={
              <div className="rounded border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-xs text-mast-warn">
                {LASER_NOTE}
              </div>
            }
          />
          <SkillRunButton skill="GetSpectrometerStatus" label={tr("读光谱仪状态", "Read spectrometer status")} immediate params={() => ({})} />
        </Group>

        <Group title={tr("光谱仪设置", "Spectrometer settings")} note={tr("采集进行中会被拒绝。换光栅约 10–30 s；换完波长轴会变，下一条谱自动带新轴。", "Refused while an acquisition is running. Changing the grating takes about 10–30 s; the wavelength axis changes afterwards and the next spectrum carries the new axis automatically.")}>
          <div className="w-32">
            <Field label={tr("中心波长 (nm)", "Center wavelength (nm)")}>
              <NumberField value={center} onChange={setCenter} />
            </Field>
          </div>
          <SkillRunButton
            skill="SetSpectrometerCenter"
            label={tr("设中心波长", "Set center wavelength")}
            params={() => ({ center_nm: parseNum(center) })}
            validate={() => {
              const v = parseNum(center);
              return v === null || Number.isNaN(v) ? tr("填中心波长（nm）", "Enter the center wavelength (nm)") : null;
            }}
          />
          <div className="w-48">
            <Field label={tr("光栅（设备上的名字）", "Grating (name on the device)")}>
              <TextField value={grating} onChange={setGrating} placeholder={tr("见「读光谱仪状态」", "See \"Read spectrometer status\"")} />
            </Field>
          </div>
          <SkillRunButton
            skill="SetSpectrometerGrating"
            label={tr("换光栅", "Change grating")}
            params={() => ({ grating: grating.trim() })}
            validate={() => (grating.trim() ? null : tr("填光栅名", "Enter the grating name"))}
          />
          <div className="w-36">
            <Field label={tr("出口", "Exit port")}>
              <SelectField
                value={port}
                onChange={setPort}
                options={[
                  { value: "front", label: "front (CCD)" },
                  { value: "side", label: "side (PMT)" },
                ]}
              />
            </Field>
          </div>
          <SkillRunButton skill="SetSpectrometerExitPort" label={tr("切出口", "Switch exit port")} params={() => ({ port })} />
        </Group>

        <div className="border-t border-mast-border pt-3">
          <div className="mb-1 text-xs font-semibold text-mast-text">{tr("偏振 · Picomotor · XYZ 台 · Nanonis 光学组 · 光学台 · 对光相机", "Polarization · Picomotor · XYZ stage · Nanonis optics group · optical stage · alignment camera")}</div>
          <p className="text-xs text-mast-muted">
            {tr("这些装置的按钮在", "Buttons for these devices are in")}{" "}
            <Link to={`/experimental/optics#${MANUAL_SECTION_ID}`} className="font-medium text-mast-accent hover:underline">
              {tr("光学台 → 手动控制", "Optics → Manual control")}
            </Link>
            {tr("：每台一张卡，解锁后可以点动、按住连续走，页头有「停止全部光学运动」。", ": one card per device; once unlocked you can jog or hold to move continuously, and the page header has \"Stop all optical motion\".")}
          </p>
        </div>
      </div>
    </Card>
  );
}
