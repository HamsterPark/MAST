import { useEffect } from "react";
import { useLocation } from "react-router-dom";
import { Section } from "@/components/ui";
import OpticsPanel from "@/components/optics/OpticsPanel";
import { StreamPanel } from "@/components/live/StreamPanel";
import { LiveConnectionBadge } from "@/components/live/LiveConnectionBadge";
import { MANUAL_SECTION_ID, ManualControlSection } from "@/components/manual/ManualControlSection";
import { STREAM_HINTS } from "@/lib/live/pick";
import { tr } from "@/lib/i18n";

// The "Manual control" section (contract section 13.1) comes first: the Controls card
// on /ters points all device buttons here (/experimental/optics#manual) so there is
// only one copy.

// Live data on the optics page (contract section 13): pump-probe and raster scans
// subscribe to the LiveHub. Stream names are defined by the skills
// (skills/builtins/optics_pump_probe.py, optics_scan.py):
//   PumpProbeScan     -> local/pump_probe/<run_id>   single stage: xy (delay vs signal), two stages: map2d
//   OpticalStageScan  -> local/optics_scan/<run_id>  2D: map2d, 1D: xy
//   AcquireSignalPoint-> local/signal_point          scalar
// (Streams local to the hub always carry the local/ prefix, contract section 7; panels
// match on the purpose segment of the name, not the prefix.)
// Panels follow the latest run by default; earlier runs stay selectable in the
// dropdown while the server still retains them.
const SIGNAL_POINT = /signal_point/;

export default function OpticsPage() {
  const loc = useLocation();
  useEffect(() => {
    if (loc.hash === `#${MANUAL_SECTION_ID}`) document.getElementById(MANUAL_SECTION_ID)?.scrollIntoView({ block: "start" });
  }, [loc.hash]);

  return (
    <>
      <div id={MANUAL_SECTION_ID} className="scroll-mt-2">
        <Section
          title={tr("手动控制", "Manual control")}
          subtitle={tr("所有电动装置的基本按钮与对光相机 · 解锁后可点动 / 按住连续走 · 停止免令牌", "Basic buttons for all motorized devices and the alignment camera · jog or hold to move once unlocked · Stop needs no token")}
        >
          <ManualControlSection />
        </Section>
      </div>
      <Section
        title={tr("光学台工具", "Optics tools")}
        subtitle={tr("原有面板：延迟线 · 单轴控制 · Wiggle 自检 · 泵浦-探测空跑", "Original panels: delay line · single-axis control · wiggle self-test · pump-probe dry run")}
      >
        <OpticsPanel />
      </Section>
      <Section
        title={tr("实时数据", "Live data")}
        subtitle={tr("泵浦-探测与光栅扫描的实时曲线和图（LiveHub）", "Live curves and maps of pump-probe and raster scans (LiveHub)")}
        actions={<LiveConnectionBadge />}
      >
        <div className="grid gap-4 2xl:grid-cols-2">
          <StreamPanel
            title={tr("泵浦-探测", "Pump-probe")}
            kind={["xy", "map2d"]}
            hint={STREAM_HINTS.pumpProbe}
            strict
            emptyLabel={tr("还没有泵浦-探测数据 —— 运行 PumpProbeScan 后，延时曲线会逐点长出来（双台扫描为二维图）。", "No pump-probe data yet. After running PumpProbeScan the delay curve grows point by point (a two-stage scan gives a 2D map).")}
          />
          <StreamPanel
            title={tr("光栅扫描", "Raster scan")}
            kind={["map2d", "xy"]}
            hint={STREAM_HINTS.raster}
            strict
            emptyLabel={tr("还没有扫描数据 —— 运行 OpticalStageScan 后，扫描图会逐像素长出来。", "No scan data yet. After running OpticalStageScan the map grows pixel by pixel.")}
          />
        </div>
        <div className="mt-4">
          <StreamPanel
            title={tr("单点信号", "Single-point signal")}
            kind="scalar"
            hint={SIGNAL_POINT}
            strict
            compact
            emptyLabel={tr("AcquireSignalPoint 每测一点，这里多一个读数。", "Each AcquireSignalPoint measurement adds a reading here.")}
          />
        </div>
      </Section>
    </>
  );
}
