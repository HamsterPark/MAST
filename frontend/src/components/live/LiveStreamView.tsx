// One stream = a title row + the default renderer chosen by kind.
//
// This is where "every new acquisition is visible live" lands: any stream declared
// in the LiveHub gets a renderer by its kind. The five known kinds have dedicated
// charts, unknown ones show their raw text. A newly declared stream is visible
// without any frontend change.

import type { ReactNode } from "react";
import clsx from "clsx";
import { Badge } from "@/components/ui";
import { fmtAgo, useLiveTick, useNow } from "@/hooks/useLive";
import type { StreamState } from "@/lib/live/store";
import { tr } from "@/lib/i18n";
import { LiveSpectrumChart } from "./LiveSpectrumChart";
import { LiveSeriesChart } from "./LiveSeriesChart";
import { LiveMap2d } from "./LiveMap2d";
import { LiveStatusLog } from "./LiveStatusLog";
import { LiveRawView } from "./LiveRawView";

export const KIND_LABEL: Record<string, string> = {
  spectrum: tr("光谱", "Spectrum"),
  scalar: tr("时间序列", "Time series"),
  xy: tr("曲线", "Curve"),
  map2d: tr("二维图", "2D map"),
  status: tr("状态", "Status"),
};

export function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind;
}

/** Pick the renderer by kind (without the title). */
export function LiveStreamBody({ st, compact = false }: { st: StreamState; compact?: boolean }) {
  const id = st.meta.id;
  switch (st.meta.kind) {
    case "spectrum":
      return <LiveSpectrumChart streamId={id} compact={compact} />;
    case "scalar":
      return <LiveSeriesChart streamId={id} mode="scalar" compact={compact} />;
    case "xy":
      return <LiveSeriesChart streamId={id} mode="xy" compact={compact} />;
    case "map2d":
      return <LiveMap2d streamId={id} compact={compact} />;
    case "status":
      return <LiveStatusLog streamId={id} compact={compact} />;
    default:
      return <LiveRawView streamId={id} />;
  }
}

/** Title row: name, id, kind, source, freshness, and the missing / lost / invalid counts, which must not be hidden. */
export function LiveStreamHeader({ st, right }: { st: StreamState; right?: ReactNode }) {
  useLiveTick(st.meta.id, 1);
  const now = useNow(1000);
  const missing = st.missing.total();
  const active = !st.meta.ended && !st.gone;
  return (
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium text-mast-text">{st.meta.title}</span>
          <Badge>{kindLabel(st.meta.kind)}</Badge>
          {st.meta.ended && <Badge tone="INFO">{tr("已结束", "Ended")}</Badge>}
          {st.gone && <Badge tone="WARN">{tr("服务端已不保留", "No longer kept by the server")}</Badge>}
          {missing > 0 && <Badge tone="WARN">{tr(`缺 ${missing} 帧 · 补齐中`, `${missing} frame(s) missing · backfilling`)}</Badge>}
          {st.lost > 0 && <Badge tone="DANGEROUS">{tr(`丢 ${st.lost} 帧（补不回）`, `${st.lost} frame(s) lost (unrecoverable)`)}</Badge>}
          {st.invalid > 0 && <Badge tone="WARN">{tr(`${st.invalid} 帧格式不对`, `${st.invalid} frame(s) malformed`)}</Badge>}
          {st.resets > 0 && <Badge tone="INFO">{tr(`服务端重启过 ${st.resets} 次`, `Server restarted ${st.resets} time(s)`)}</Badge>}
        </div>
        <div className="mt-0.5 flex flex-wrap gap-x-3 font-mono text-[11px] text-mast-faint">
          <span>{st.meta.id}</span>
          {st.meta.source && <span>{tr("来源", "source")} {st.meta.source}</span>}
          <span>
            #{st.cursor} · {tr(`${st.received} 帧`, `${st.received} frame(s)`)}
          </span>
          <span className={clsx(active && st.lastRecvMs !== null && now - st.lastRecvMs > 15_000 && "text-mast-warn")}>
            {tr("最后更新", "Last update")} {fmtAgo(st.lastRecvMs, now)}
          </span>
        </div>
      </div>
      {right}
    </div>
  );
}
