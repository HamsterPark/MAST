import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { ErrorNote, Spinner } from "../ui";
import type { SettingsPatch } from "@/lib/settingsWrite";
import { getLang, tr } from "@/lib/i18n";

// ── Z controller presets ────────────────────────────────────────────────────
//
// An agent sets Z feedback parameters by naming a preset (ApplyZCtrlPreset);
// the numbers are looked up by code from the sources listed here and written to
// the hardware. This section answers two questions: which presets exist, and
// where each preset's numbers come from.
//
// Why numbers are kept out of the model's vocabulary: a value like p_gain=3e-12
// can lose its exponent in transit and arrive as 3, a legal float that no
// validation layer rejects but that is 10^12 times too large. With the numbers
// removed from the model's interface, what remains is letting the user SEE what
// the system will write, which is this section.
//
// It is deliberately read-only plus delete, with no edit fields:
//   - the approach preset's values are edited under Instrument profile ->
//     approach parameter set;
//   - the scan preset and the per-tier names are edited in the scan tier table
//     below;
//   - custom presets are created by the agent through a human approval card.
// Each number has exactly one editable home and this section is the summary.
// A second set of inputs here could silently disagree with the real one.

type Preset = {
  name: string;
  kind: "reserved" | "custom" | "scan-tier";
  usable: boolean;
  why?: string | null;
  p_gain?: number | null;
  i_gain?: number | null;
  time_constant_s?: number | null;
  setpoint_a?: number | null;
  sources?: Record<string, string> | null;
};

type CustomPreset = {
  name: string;
  p_gain: string;
  i_gain: string;
  setpoint_a?: string | null;
  note?: string | null;
};

type PresetsPayload = {
  presets: Preset[];
  custom: CustomPreset[];
  reserved: string[];
  max_presets: number;
};

/** 3e-12 -> "3p". Same notation as the backend core/si_quantity.format_si. */
function si(value: number | null | undefined, unit: string): string {
  if (value === null || value === undefined) return "—";
  const v = Number(value);
  if (!Number.isFinite(v)) return "—";
  if (v === 0) return `0${unit}`;
  const steps: [string, number][] = [
    ["G", 1e9], ["M", 1e6], ["k", 1e3],
    ["m", 1e-3], ["u", 1e-6], ["n", 1e-9], ["p", 1e-12], ["f", 1e-15],
  ];
  for (const [prefix, factor] of steps) {
    const scaled = v / factor;
    if (Math.abs(scaled) >= 1 && Math.abs(scaled) < 1000) {
      return `${Number(scaled.toPrecision(6))}${prefix}${unit}`;
    }
  }
  return `${v.toPrecision(4)}${unit}`;
}

const KIND_LABEL: Record<Preset["kind"], string> = {
  reserved: tr("内置", "Built-in"),
  "scan-tier": tr("扫描档位", "Scan tier"),
  custom: tr("自定义", "Custom"),
};

function usePresets() {
  return useQuery({
    queryKey: ["settings", "zctrl-presets"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/settings/zctrl-presets");
      if (error) throw error;
      return data as unknown as PresetsPayload;
    },
  });
}

export function ZCtrlPresetsSection({
  saving,
  save,
}: {
  saving: boolean;
  save: (patch: SettingsPatch) => void;
}) {
  const { data, isLoading, error } = usePresets();

  const removeCustom = (name: string) => {
    const rest = (data?.custom ?? []).filter((p) => p.name !== name);
    save({ zctrl_presets: rest });
  };

  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-sm font-semibold">{tr("Z 参数组", "Z controller presets")}</h3>
        <p className="text-xs text-mast-muted">
          {getLang() === "zh" ? (
            <>
              AI 设置 Z 反馈参数时只说组名，<b>具体数值由代码取出并写入硬件，不经过模型</b>，
              写完自动回读比对，不一致即报失败。这里是它能用的全部组，以及每组的数从哪里取。
            </>
          ) : (
            <>
              When an agent sets Z feedback parameters it only names a preset;{" "}
              <b>the values are looked up by code and written to the hardware without passing
              through the model</b>. After writing, they are read back and compared, and any
              mismatch is reported as a failure. These are all the presets it can use, and where
              each preset's numbers come from.
            </>
          )}
        </p>
      </div>

      {isLoading && <Spinner />}
      {error != null && <ErrorNote error={error} />}

      {data != null && (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-mast-muted">
                <tr className="text-left">
                  <th className="py-1 pr-3 font-medium">{tr("组名", "Preset")}</th>
                  <th className="py-1 pr-3 font-medium">{tr("来源", "Source")}</th>
                  <th className="py-1 pr-3 font-medium">P (m)</th>
                  <th className="py-1 pr-3 font-medium">I (m/s)</th>
                  <th className="py-1 pr-3 font-medium">T = P/I</th>
                  <th className="py-1 pr-3 font-medium">{tr("设定点", "Setpoint")}</th>
                  <th className="py-1 font-medium" />
                </tr>
              </thead>
              <tbody>
                {data.presets.map((p) => (
                  <tr key={p.name} className="border-t border-mast-border/40 align-top">
                    <td className="py-1.5 pr-3">
                      <code className="font-medium">{p.name}</code>
                    </td>
                    <td className="py-1.5 pr-3 text-mast-muted">{KIND_LABEL[p.kind]}</td>
                    {p.usable ? (
                      <>
                        <td className="py-1.5 pr-3 tabular-nums">{si(p.p_gain, "")}</td>
                        <td className="py-1.5 pr-3 tabular-nums">{si(p.i_gain, "")}</td>
                        <td className="py-1.5 pr-3 tabular-nums">
                          {si(p.time_constant_s, "s")}
                        </td>
                        <td className="py-1.5 pr-3 tabular-nums">
                          {p.setpoint_a == null ? tr("不改", "Unchanged") : si(p.setpoint_a, "A")}
                        </td>
                      </>
                    ) : (
                      <td className="py-1.5 pr-3 text-mast-muted" colSpan={4}>
                        {p.name === "scan"
                          ? tr("应用时按当前图幅自动选档", "Tier is chosen from the current frame size when applied")
                          : tr(`未配置 — ${p.why ?? ""}`, `Not configured — ${p.why ?? ""}`)}
                      </td>
                    )}
                    <td className="py-1.5 text-right">
                      {p.kind === "custom" && (
                        <button
                          type="button"
                          disabled={saving}
                          onClick={() => removeCustom(p.name)}
                          className="text-mast-muted hover:text-red-400 disabled:opacity-50"
                        >
                          {tr("删除", "Delete")}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <p className="text-[11px] text-mast-muted">
            {getLang() === "zh" ? (
              <>
                改哪里：<code>approach</code> 在上面「仪器档案 → 进针参数组」；
                <code>scan</code> 与各扫描档名在下面「扫描档位表」；
                自定义组由 AI 新建（每次都要你在人工审批卡上过目，卡片上显示的就是将要存进去的值）。
                最多 {data.max_presets} 个自定义组。
              </>
            ) : (
              <>
                Where to edit: <code>approach</code> is under "Instrument profile → approach
                parameter set" above; <code>scan</code> and the scan tier names are in the "Scan
                tier table" below; custom presets are created by the agent and always need your
                approval on a human approval card, which shows the exact values to be stored. At
                most {data.max_presets} custom presets.
              </>
            )}
          </p>
        </>
      )}
    </section>
  );
}
