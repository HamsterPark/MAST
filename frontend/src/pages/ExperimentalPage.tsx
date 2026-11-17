import { Section } from "@/components/ui";
import { SubTabs } from "@/components/controls";
import { SignalCapturePanel } from "@/components/experimental/SignalCapturePanel";
import { MonitorPanel } from "@/components/vision/MonitorPanel";
import { MosaicPanel } from "@/components/experimental/MosaicPanel";
import { useStickyTab } from "@/hooks/useStickyTab";
import { useStationCapabilities } from "@/hooks/useStationCapabilities";
import { LegacyOnly } from "@/components/shell/LegacyOnly";
import { tr } from "@/lib/i18n";
import type { Capability } from "@/lib/stationCapabilities";

// Experimental tools. Three sub-functions dispatched by flat in-page SubTabs:
//   Signal capture + FFT: GET /api/experimental/signals (channels + units + Osci
//                    timebases + FFT enums) + POST /api/experimental/fft. Panel
//                    features: multi-channel overlay, N-average denoising with a
//                    counter, peak readout table, frequency-axis scaling (linear/log
//                    X + log Y), live continuous mode, unit scaling, acquisition
//                    presets (localStorage) and time-domain / spectrum CSV/PNG export
//   Long-term monitoring: POST monitor/start|stop, GET monitor/status (reused
//                    MonitorPanel; vision-model based, so legacy-only)
//   Mosaic:         POST /api/experimental/mosaic (big-canvas overview PNG)
// Direct hardware tools (no chat agent in the loop); large sample/ndarray data
// is rendered/exported by the core and never crosses the wire.
//
// The first two read the Nanonis oscilloscope, so they exist only on a station with the
// `nanonis` capability; the mosaic stitches .sxm files already saved in a folder and works
// on any station. The tab list follows `has()` (an STM station's list from the first frame);
// the two Nanonis panels, which ask the instrument for its channels as soon as they mount,
// mount only once the station has said it has Nanonis.

type SubTab = "capture" | "monitor" | "mosaic";

// One list feeds both the tab bar and useStickyTab's allowed ids; a second copy would
// drift, and the symptom (a sub-page that is not remembered) is hard to trace back to
// the storage key.
const SUB_TABS: { id: SubTab; label: string; requires?: Capability }[] = [
  { id: "capture", label: tr("信号捕获 + FFT", "Signal capture + FFT"), requires: "nanonis" },
  { id: "monitor", label: tr("长期监控", "Long-term monitoring"), requires: "nanonis" },
  { id: "mosaic", label: tr("图像拼接", "Mosaic") },
];

export default function ExperimentalPage() {
  const station = useStationCapabilities();
  const nanonis = station.has("nanonis");
  const nanonisReads = station.ready && nanonis;
  const tabs = SUB_TABS.filter((t) => t.requires === undefined || station.has(t.requires));
  const [remembered, setTab] = useStickyTab<SubTab>(
    "experimental", tabs.map((t) => t.id), tabs[0]?.id ?? "mosaic");
  // useStickyTab checks the remembered tab once, on the first render; the station may have
  // said what it has only after that, so the check is repeated here.
  const tab: SubTab = tabs.some((t) => t.id === remembered) ? remembered : (tabs[0]?.id ?? "mosaic");
  return (
    <Section title={tr("实验性功能", "Experimental")}>
      <p className="mb-3 text-xs text-mast-muted">
        {nanonis
          ? tr(
              "实验性功能 / Experimental — 直接调用仪器 skill 的图形化工具，不经过对话 agent。⚠️ 直连 Nanonis 硬件，操作前请确认仪器状态。",
              "Experimental: graphical tools that call instrument skills directly, without a chat agent. ⚠️ These talk to the Nanonis hardware directly; check the instrument state before use.",
            )
          : tr(
              "实验性功能 / Experimental — 图像拼接：把一个文件夹里保存的 .sxm 扫描拼成一张总览图。只读文件，不碰仪器。",
              "Experimental: the mosaic stitches the .sxm scans saved in a folder into one overview. It only reads files and never touches an instrument.",
            )}
      </p>
      <SubTabs<SubTab> value={tab} onChange={setTab} tabs={tabs} />
      {tab === "capture" && nanonisReads && <SignalCapturePanel />}
      {tab === "monitor" && nanonisReads && (
        <LegacyOnly compact>
          <MonitorPanel />
        </LegacyOnly>
      )}
      {tab === "mosaic" && <MosaicPanel />}
    </Section>
  );
}
