// Header "Stop all optics motion" (contract §13.1): satellite devices + the optical stage on the
// STM computer + all Nanonis motor groups. All three are token-free stop skills
// (instrument_lock.BYPASS_NAMES) and are **all called in turn**: if one fails, throws, or even
// the backend is unreachable, the following ones are still called, so one failed stop never
// leaves another thing running.
//
// StopMotor stops **all** Nanonis motor groups (Motor_StopMove has no group parameter), the STM
// coarse motor included; that is what "stop all" must do, and it is stated next to the button.

import { tr } from "../i18n.ts";

export interface StopStep {
  skill: string;
  params: Record<string, unknown>;
  label: string;
}

export const STOP_ALL_STEPS: readonly StopStep[] = [
  { skill: "StopSatelliteDevices", params: {}, label: tr("卫星上的全部设备", "all devices on the satellite") },
  { skill: "StopOpticalStage", params: {}, label: tr("STM 电脑上的全部光学台", "all optical stages on the STM computer") },
  { skill: "StopMotor", params: {}, label: tr("Nanonis 全部电机组", "all Nanonis motor groups") },
];

export interface StopStepResult extends StopStep {
  ok: boolean;
  message: string;
}

export type StopRunner = (skill: string, params: Record<string, unknown>) => Promise<{ ok: boolean; message: string }>;

export async function stopAllOptics(run: StopRunner, steps: readonly StopStep[] = STOP_ALL_STEPS): Promise<StopStepResult[]> {
  const out: StopStepResult[] = [];
  for (const s of steps) {
    try {
      const r = await run(s.skill, s.params);
      out.push({ ...s, ok: r.ok, message: r.message });
    } catch (e) {
      out.push({ ...s, ok: false, message: tr(`没有送达：${e instanceof Error ? e.message : String(e)}`, `Not delivered: ${e instanceof Error ? e.message : String(e)}`) });
    }
  }
  return out;
}
