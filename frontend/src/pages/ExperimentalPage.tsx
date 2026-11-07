import { Section } from "@/components/ui";
import { SubTabs } from "@/components/controls";
import { SignalCapturePanel } from "@/components/experimental/SignalCapturePanel";
import { MonitorPanel } from "@/components/vision/MonitorPanel";
import { MosaicPanel } from "@/components/experimental/MosaicPanel";
import { useStickyTab } from "@/hooks/useStickyTab";
import { LegacyOnly } from "@/components/shell/LegacyOnly";
import { tr } from "@/lib/i18n";

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

type SubTab = "capture" | "monitor" | "mosaic";

// One list feeds both the tab bar and useStickyTab's allowed ids; a second copy would
// drift, and the symptom (a sub-page that is not remembered) is hard to trace back to
// the storage key.
const SUB_TABS: { id: SubTab; label: string }[] = [
  { id: "capture", label: tr("信号捕获 + FFT", "Signal capture + FFT") },
  { id: "monitor", label: tr("长期监控", "Long-term monitoring") },
  { id: "mosaic", label: tr("图像拼接", "Mosaic") },
];

export default function ExperimentalPage() {
  const [tab, setTab] = useStickyTab<SubTab>(
    "experimental", SUB_TABS.map((t) => t.id), "capture");
  return (
    <Section title={tr("实验性功能", "Experimental")}>
      <p className="mb-3 text-xs text-mast-muted">
        {tr(
          "实验性功能 / Experimental — 直接调用仪器 skill 的图形化工具，不经过对话 agent。⚠️ 直连 Nanonis 硬件，操作前请确认仪器状态。",
          "Experimental: graphical tools that call instrument skills directly, without a chat agent. ⚠️ These talk to the Nanonis hardware directly; check the instrument state before use.",
        )}
      </p>
      <SubTabs<SubTab> value={tab} onChange={setTab} tabs={SUB_TABS} />
      {tab === "capture" && <SignalCapturePanel />}
      {tab === "monitor" && (
        <LegacyOnly compact>
          <MonitorPanel />
        </LegacyOnly>
      )}
      {tab === "mosaic" && <MosaicPanel />}
    </Section>
  );
}
