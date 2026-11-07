// Figure entry point in the lightbox sidebar: frame -> "comparison figure", grid spectrum -> "per-layer figure".
//
// A separate component rather than part of Lightbox: Lightbox must call all its hooks before
// `if (!it) return null`, and adding useQueryClient there would be one more call that has to sit
// before the early return.

import { useQueryClient } from "@tanstack/react-query";
import type { GalleryItem } from "@/lib/gallery/types";
import { DARK_BTN } from "../bits";
import { startFigureJob } from "./figureJob";
import { tr } from "@/lib/i18n";

export function LightboxFigureButton({ it }: { it: GalleryItem }) {
  const qc = useQueryClient();
  if (it.k !== "f" && it.k !== "g") return null;
  const isFrame = it.k === "f";
  return (
    <div className="mt-2">
      <button
        type="button"
        className={DARK_BTN}
        title={
          isFrame
            ? tr(
                "「原版（只减平面）| 逐行调平」并排的一张图，底部写时刻、偏压、电流、尺寸",
                "One figure showing \"original (plane subtraction only) | line-by-line flattened\" side by side, with time, bias, current and size written at the bottom",
              )
            : tr(
                "形貌 + 每一个偏压层的 dI/dV 与电流，每层各自拉伸",
                "Topography plus dI/dV and current for every bias layer, each layer stretched independently",
              )
        }
        onClick={() =>
          void startFigureJob(qc, isFrame ? { kind: "frame_sheet", ids: [it.id] } : { kind: "grid_sheets", ids: [it.id] })
        }
      >
        {isFrame ? tr("出对比图", "Make comparison sheet") : tr("出逐层图", "Make layer sheets")}
      </button>
    </div>
  );
}
