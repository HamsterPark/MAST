// Spectroscopy / TERS (contract section 13).
//
// Left: data (live spectrum, the TERS map being built, PMT). Right: state and controls
// (satellites, buttons, axis positions). At the bottom: the stream list, where every
// LiveHub stream is drawn automatically by kind, so a newly added acquisition type
// shows up without changing this page (contract section 3, decision 5).
//
// The three panels above follow the latest stream by default: the best-matching name
// among streams that are active and receiving data (sticky, so it does not flip
// between two streams that are both receiving). Streams they are drawing are
// collapsed in the list below so the same chart is not drawn twice.
//
// Live spectrum: the satellite forwards every CCD frame (<sat>/<dev>/spectrum,
// including manual acquisitions), and AcquireSpectrum additionally publishes the
// averaged spectrum of each acquisition (local/spectra/<dev>). When both are
// arriving, the panel shows the one MAST acquired and the other stays in the list.

import { useMemo, useState } from "react";
import { Section } from "@/components/ui";
import { STREAM_HINTS } from "@/lib/live/pick";
import { StreamPanel } from "@/components/live/StreamPanel";
import { LiveStreamList } from "@/components/live/LiveStreamList";
import { LiveConnectionBadge } from "@/components/live/LiveConnectionBadge";
import { SatellitePanel } from "@/components/ters/SatellitePanel";
import { LASER_NOTE, TersControls } from "@/components/ters/TersControls";
import { AxisPanel } from "@/components/ters/AxisPanel";
import { tr } from "@/lib/i18n";

type PanelKey = "spectrum" | "map" | "pmt";

export default function TersPage() {
  const [panels, setPanels] = useState<Record<PanelKey, string | null>>({ spectrum: null, map: null, pmt: null });
  const on = useMemo(() => {
    const make = (key: PanelKey) => (id: string | null) =>
      setPanels((p) => (p[key] === id ? p : { ...p, [key]: id }));
    return { spectrum: make("spectrum"), map: make("map"), pmt: make("pmt") };
  }, []);
  const shown = Object.values(panels).filter((id): id is string => id !== null);

  return (
    <Section
      title={tr("光谱 / TERS", "Spectroscopy / TERS")}
      subtitle={tr("卫星节点 · 实时光谱 · 轴位置 · TERS 图 · 全部实时数据流", "Satellite nodes · live spectrum · axis positions · TERS map · all live streams")}
      actions={<LiveConnectionBadge />}
    >
      <div
        role="note"
        className="mb-4 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-sm text-mast-warn"
      >
        {LASER_NOTE}
      </div>

      <div className="grid gap-4 2xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="min-w-0 space-y-4">
          <StreamPanel
            title={tr("实时光谱", "Live spectrum")}
            kind="spectrum"
            hint={STREAM_HINTS.spectrum}
            prefer={STREAM_HINTS.acquired}
            onShown={on.spectrum}
            emptyLabel={tr("还没有光谱流 —— 卫星连上并开始采集（或运行 AcquireSpectrum）后自动出现。", "No spectrum stream yet. It appears automatically once the satellite connects and starts acquiring (or after running AcquireSpectrum).")}
          />
          <StreamPanel
            title={tr("正在构建的 TERS 图 / 对准图", "TERS map / alignment map in progress")}
            kind="map2d"
            hint={STREAM_HINTS.tersMap}
            onShown={on.map}
            emptyLabel={tr("还没有二维图 —— TERS 扫图、对准扫描开始后逐像素长出来。", "No 2D map yet. It grows pixel by pixel once a TERS map or alignment scan starts.")}
          />
          <StreamPanel
            title="PMT"
            kind="scalar"
            hint={STREAM_HINTS.pmt}
            strict
            onShown={on.pmt}
            emptyLabel={tr("没有 PMT 流（SpectraSense / PMT 默认关闭）。", "No PMT stream (SpectraSense / PMT is off by default).")}
          />
        </div>
        <div className="min-w-0 space-y-4">
          <SatellitePanel />
          <TersControls />
          <AxisPanel />
        </div>
      </div>

      <div className="mt-6">
        <Section title={tr("数据流", "Data streams")} subtitle={tr("LiveHub 里声明过的每一个流，按类型自动挂上默认画法", "Every stream declared in the LiveHub, drawn by default according to its type")}>
          <LiveStreamList shownElsewhere={shown} />
        </Section>
      </div>
    </Section>
  );
}
