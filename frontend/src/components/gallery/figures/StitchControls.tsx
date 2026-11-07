// Starting a single-spectrum stitch: shared by the bottom selection bar (when all selected items
// are spectra) and the Marked page's "single spectra in no series" list.
//
// κ is only used to annotate the theoretical factor exp(2κΔz) for the Zoff difference on the
// figure; the stitching factor itself comes from the measured current ratio near the seams. So
// it is a figure parameter, not an instrument setting.

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { parseKappa } from "@/lib/gallery/figures";
import { SMALL_BTN, SMALL_SELECT } from "../bits";
import { toast } from "../toast";
import { startFigureJob } from "./figureJob";
import { tr } from "@/lib/i18n";

export const DEFAULT_KAPPA = "";

export function KappaInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <label
      className="flex items-center gap-1"
      title={tr(
        "隧穿衰减常数 κ：只用来在图上标注 Zoff 差的理论倍数 exp(2κΔz)；默认 11.4 nm⁻¹",
        "Tunneling decay constant κ: only used to annotate the theoretical factor exp(2κΔz) for the Zoff difference on the figure; default 11.4 nm⁻¹",
      )}
    >
      κ
      <input className={`${SMALL_SELECT} w-14`} value={value} onChange={(e) => onChange(e.target.value)} />
      nm⁻¹
    </label>
  );
}

/** Empty `ids` with `groupByDir` => the server stitches all marked spectra that are in no series, one figure per directory. */
export function useStitchStarter() {
  const qc = useQueryClient();
  return (ids: string[], kappaText: string, groupByDir = false) => {
    const kappa = parseKappa(kappaText);
    if (kappaText.trim() && kappa == null) {
      toast(tr("κ 要是正数（nm⁻¹）", "κ must be a positive number (nm⁻¹)"), "err");
      return;
    }
    const options: Record<string, unknown> = kappa == null ? {} : { kappa_per_nm: kappa };
    if (groupByDir) options.group_by_dir = true;
    void startFigureJob(qc, { kind: "sts_stitch", ids, options });
  };
}

/** The "Stitch figure" control on the bottom selection bar. */
export function StitchSelected({ ids }: { ids: string[] }) {
  const [kappa, setKappa] = useState(DEFAULT_KAPPA);
  const start = useStitchStarter();
  return (
    <span className="inline-flex items-center gap-1.5">
      <button
        type="button"
        className={SMALL_BTN}
        title={tr(
          "把选中的谱拼成一条宽范围曲线：覆盖 0 V、sweep×点数最多的一段为核心，其余各段按接缝附近的电流比乘系数接上",
          "Stitch the selected spectra into one wide-range curve: the segment covering 0 V with the most sweeps × points is the core; the others are joined with a factor from the current ratio near the seams",
        )}
        onClick={() => start(ids, kappa)}
      >
        {tr("拼接出图", "Stitch figure")}
      </button>
      <KappaInput value={kappa} onChange={setKappa} />
    </span>
  );
}
