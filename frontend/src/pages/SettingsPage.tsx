import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import { api } from "../api/client";
import { useUiStore } from "../store";
import { getLang, tr } from "@/lib/i18n";
import { RECOMMENDED_MODEL_LABEL } from "@/lib/project";
import { useLegacyFeatures } from "@/hooks/useLegacyFeatures";
import { useStationProfile } from "@/hooks/useStationProfile";
import { profileBlurb, profileLabel, type StationProfile } from "@/lib/stationProfile";
import { openOnboardingGuide } from "@/components/shell/OnboardingGuide";
import { VacuumInterlockStrip } from "@/components/vision/VacuumInterlockStrip";
import { INSTRUMENT_INIT_KEY, useInstrumentInit } from "@/hooks/useInstrumentInit";
import { SignalIndexField } from "@/components/signals/SignalIndexField";
import { SIGNAL_AUTO_VALUE, SIGNAL_INDEX_KEYS } from "@/lib/signalChannels";
import { choiceToSave, declaredChoiceView, type ChoiceOption } from "@/lib/declaredChoice";
import {
  Badge,
  DegradedNote,
  EmptyNote,
  ErrorNote,
  Section,
  Spinner,
} from "../components/ui";
import {
  Accordion,
  Checkbox,
  Field,
  RadioGroup,
  SelectField,
  Toggle,
  useToast,
} from "../components/controls";
import { ModelCapabilityTable } from "../components/settings/ModelCapabilityTable";
import { StationProfileDialog } from "../components/settings/StationProfileDialog";
import { AgentOverridesEditor } from "../components/settings/AgentOverridesEditor";
// Hardware connection in Settings is read-only: a small port table shows the
// resolved host and the 4 Nanonis TCP ports. Editing, reconnecting and sensors live
// under Advanced -> Hardware (PIN-gated). admin/HardwareManager is deliberately not
// reused here so this page never edits hardware.
import { NanonisPortsReadOnly } from "../components/settings/NanonisPortsReadOnly";
import { DeviceScanButton, InstrumentStateCard } from "@/components/admin/DeviceScanner";
import { RemoteAccessSection } from "../components/settings/RemoteAccessSection";
import { MonitoringSettingsSection } from "../components/settings/MonitoringSettingsSection";
import { EnvHistorySettingsSection } from "../components/settings/EnvHistorySettingsSection";
import { SatelliteSettingsSection } from "@/components/settings/SatelliteSettingsSection";
import { ConductSettingsSection } from "@/components/settings/ConductSettingsSection";
import { CONDUCT_SETTINGS_TITLE } from "@/lib/conduct";
import { ScanPolicySection } from "../components/settings/ScanPolicySection";
import { ZCtrlPresetsSection } from "../components/settings/ZCtrlPresetsSection";
import { settingsWriteProblem, type SettingsPatch } from "@/lib/settingsWrite";

// Settings page: a stack of collapsible sections (not sub-tabs), each persisted
// through POST /api/settings (or the model-override endpoint).
//   Model         — global model, thinking (read-only, shows the locked value),
//                   query-assistant model, per-agent overrides
//   Appearance    — font size, theme, live skill search
//   Voice         — TTS voice and autoplay
//   Other         — read-only notes
// Sections that only concern the built-in agents are hidden unless legacy features
// are enabled; hardware and safety sections are always shown. Hardware editing lives
// behind the PIN-gated Advanced tab.
// On an optical station (station profile "optical", no STM) the new-instrument setup
// row and the Nanonis hardware-connection section are hidden; the station-profile
// card next to the legacy-features switch is where the profile is chosen.

// ── shared queries ────────────────────────────────────────────────────────────
function useModels() {
  return useQuery({
    queryKey: ["config", "models"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/config/models");
      if (error) throw error;
      return data;
    },
  });
}

function useSettings() {
  return useQuery({
    queryKey: ["settings"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/settings");
      if (error) throw error;
      return data;
    },
  });
}

function useHardwareModules() {
  return useQuery({
    queryKey: ["settings", "hardware-modules"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/settings/hardware-modules");
      if (error) throw error;
      return data;
    },
  });
}

function useVoices() {
  return useQuery({
    queryKey: ["voice", "voices"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/voice/voices");
      if (error) throw error;
      return data;
    },
  });
}

// Query-assistant model choices. Independent of the global model.
const QA_MODEL_CHOICES: { value: string; label: string }[] = [
  { value: "kimi-k2.6", label: "kimi-k2.6  (Moonshot)" },
  { value: "kimi-k2.7-code", label: "kimi-k2.7-code  (Moonshot)" },
  { value: "deepseek-v4-pro", label: "deepseek-v4-pro  (DeepSeek)" },
  { value: "qwen3.7-max", label: "qwen3.7-max  (Qwen / DashScope)" },
  { value: "qwen3.7-max-preview", label: tr("qwen3.7-max-preview  (Qwen 快照)", "qwen3.7-max-preview  (Qwen snapshot)") },
  { value: "minimax-m3", label: "minimax-m3  (MiniMax)" },
  { value: "glm-5.2", label: "glm-5.2  (Zhipu)" },
];

// The persisted values stay 小/中/大; only the label is translated.
const FONT_CHOICES = [
  { value: "小", label: tr("小", "Small") },
  { value: "中", label: tr("中", "Medium") },
  { value: "大", label: tr("大", "Large") },
];
const THEME_CHOICES = [
  { value: "Light", label: "Light" },
  { value: "Dark", label: "Dark" },
];

// ── Appearance live-apply helpers ──────────────────────────────────────────────
// font_scale persists as 小/中/大; map to a root font-size so every rem-based
// Tailwind size scales. Applied live (no reload) and re-applied on settings load.
const FONT_PX: Record<string, string> = { 小: "14px", 中: "16px", 大: "18px" };
function applyFontScale(scale: string) {
  const root = document.documentElement;
  root.style.fontSize = FONT_PX[scale] ?? "16px";
  root.setAttribute("data-font-scale", scale);
}

// The settings theme is persisted as Light/Dark; the zustand store (top-bar toggle)
// uses lowercase dark/light. Map both ways so the Appearance control both reflects and
// drives useUiStore.theme (which AppLayout's effect applies to <html>).
type StoreTheme = "dark" | "light";
const settingsThemeToStore = (t: string): StoreTheme => (t === "Dark" ? "dark" : "light");
const storeThemeToSettings = (t: StoreTheme): string => (t === "dark" ? "Dark" : "Light");

// ── Thinking display (read-only) ───────────────────────────────────────────────
// Thinking is not user-selectable: the system locks every model to its strongest
// supported value. This helper turns a model's thinking_mode (from
// GET /api/config/models) into the read-only text/badge shown identically for the
// global model, the query assistant and each agent, so the display always matches
// the value actually in effect:
//   none    -> no thinking parameter is sent
//   fixed   -> reasoning model fixes it server-side
//   tunable -> the system locks tunable models to max
export type ThinkingMode = "none" | "fixed" | "tunable";

export function thinkingDisplay(mode: ThinkingMode | string | null | undefined): {
  label: string;
  tone: string;
} {
  switch (mode) {
    case "none":
      return { label: tr("无思考", "No thinking"), tone: "WARN" };
    case "fixed":
      return { label: tr("固定·始终开启（最强）", "Fixed · always on (strongest)"), tone: "INFO" };
    case "tunable":
    default:
      return { label: tr("max · 最强（锁定）", "max · strongest (locked)"), tone: "AUTO" };
  }
}

// tr() for JSX content: picks the Chinese or English node for the active language.
const trn = (zh: ReactNode, en: ReactNode): ReactNode => (getLang() === "zh" ? zh : en);

const LEGACY_ANCHOR = "legacy-features";
const STATION_ANCHOR = "station-profile";
const PIN_GUIDE_ANCHOR = "admin-pin-guide";

function scrollToAnchor(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

/** Inline link that scrolls to the admin-PIN guide card at the top of this page. */
function PinGuideLink({ children }: { children: ReactNode }) {
  return (
    <a
      href={`#${PIN_GUIDE_ANCHOR}`}
      onClick={(e) => {
        e.preventDefault();
        scrollToAnchor(PIN_GUIDE_ANCHOR);
      }}
      className="text-mast-accent underline-offset-2 hover:underline"
    >
      {children}
    </a>
  );
}

export default function SettingsPage() {
  const models = useModels();
  const settings = useSettings();
  const voices = useVoices();
  const hwModules = useHardwareModules();
  const queryClient = useQueryClient();
  const { toast, node: toastNode } = useToast();
  const { enabled: legacy } = useLegacyFeatures();
  const { isOptical } = useStationProfile();
  const location = useLocation();

  // Scroll to the section named by the URL hash (e.g. /settings/general#legacy-features).
  // The target renders after the settings query resolves, so re-run when it does.
  useEffect(() => {
    const id = location.hash.replace(/^#/, "");
    if (!id) return;
    const t = window.setTimeout(() => scrollToAnchor(id), 50);
    return () => window.clearTimeout(t);
  }, [location.hash, settings.isSuccess]);

  // Unified write mutation -> POST /api/settings (partial; None = leave unchanged).
  const saveSettings = useMutation({
    mutationFn: async (body: SettingsPatch) => {
      const { data, error } = await api.POST("/api/settings", { body });
      if (error) throw error;
      return data;
    },
    onSuccess: (data) => {
      // The backend reports a per-key reason in `rejected`; the shared predicate
      // surfaces it instead of a bare "save failed".
      const problem = settingsWriteProblem(data);
      if (problem) {
        toast(problem, "err");
      } else {
        // A hardware-module toggle is persisted as soon as this returns, but it is
        // not live until the agent's tool list is rebuilt (the list is frozen at
        // graph-build time). The backend reports what will actually happen, so show
        // that note instead of a bare "saved".
        const note = (data as { rebuild_note?: string })?.rebuild_note;
        toast(note ? `${tr("已保存", "Saved")}${note}` : tr("已保存", "Saved"), "ok");
        queryClient.invalidateQueries({ queryKey: ["settings"] });
      }
    },
    onError: (e) =>
      toast(`${tr("保存失败：", "Save failed: ")}${String((e as Error)?.message ?? e)}`, "err"),
  });

  const save = (patch: SettingsPatch) => saveSettings.mutate(patch);

  return (
    <div>
      <Section title={tr("设置", "Settings")}>
        <p className="mb-4 text-sm text-mast-muted">
          <strong>{tr("设置", "Settings")}</strong>{" "}
          {legacy
            ? tr(
                "— 统一配置：模型 / 外观 / 硬件连接 / 知识 / 语音。所有改动会自动持久化，重启后保留。",
                "— Unified configuration: model, appearance, hardware connection, knowledge and voice. Changes are saved automatically and survive restarts.",
              )
            : tr(
                "— 统一配置：外观 / 硬件连接 / 实验参数 / 监控。所有改动会自动持久化，重启后保留。",
                "— Unified configuration: appearance, hardware connection, experiment parameters and monitoring. Changes are saved automatically and survive restarts.",
              )}
        </p>

        <AdminPinGuideCard />

        <LegacyFeaturesCard />

        <StationProfileCard />

        {/* Manual entry to the new-instrument setup checklist. The automatic prompt is
            the top banner (shown when required values are missing, the hardware
            fingerprint changed, or setup was never run); this row is for going through
            it again on demand. Hidden on an optical station: the checklist is made of
            STM quantities. */}
        {!isOptical && <SetupEntryRow toast={toast} />}

        {legacy && (
          <Accordion title={tr("模型 Model", "Model")} defaultOpen>
            <ModelSection
              models={models}
              settings={settings}
              saving={saveSettings.isPending}
              save={save}
            />
            <div className="mt-4">
              <Accordion
                title={tr(
                  "各 Agent 模型 / Thinking（与 Agents 页同源）",
                  "Per-agent model / Thinking (same source as the Agents page)",
                )}
                defaultOpen={false}
              >
                <AgentOverridesEditor toast={toast} />
              </Accordion>
            </div>
          </Accordion>
        )}

        <Accordion title={tr("外观 Appearance", "Appearance")} defaultOpen>
          <AppearanceSection
            settings={settings}
            saving={saveSettings.isPending}
            save={save}
            legacy={legacy}
          />
        </Accordion>

        {/* Nanonis ports: not shown on an optical station (it has no Nanonis). */}
        {!isOptical && (
          <Accordion title={tr("硬件连接 Hardware", "Hardware connection")} defaultOpen>
            {/* Ports are read-only here; editing lives in Advanced -> Hardware (PIN-gated).
                The table shows host, the 4 ports (defaults 6501-6504 via
                GET /api/nanonis/connection) and live status. */}
            <NanonisPortsReadOnly />
          </Accordion>
        )}

        <Accordion title={tr("温度计 / 真空计 Instruments", "Thermometer / vacuum gauge")} defaultOpen>
          {/* Scan the serial ports, show what was identified, and connect it only
              after the user confirms in the dialog. Adoption is persisted, so the
              port is found automatically on every later boot. Below it, the read-only
              instrument readout (inputs and heater state). MAST only queries it; it
              cannot set a temperature or switch a heater on. */}
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <DeviceScanButton />
              <span className="text-xs text-mast-muted">
                {tr(
                  "扫描串口上的温控仪 / 真空计，确认后接入，并记住到下次开机。",
                  "Scan the serial ports for temperature controllers and vacuum gauges; once confirmed they are connected and remembered for the next boot.",
                )}
              </span>
            </div>
            <InstrumentStateCard />
          </div>
        </Accordion>

        <Accordion
          title={tr("远程访问 Remote（Tailscale 跨网控制）", "Remote access (Tailscale cross-network control)")}
          defaultOpen={false}
        >
          <RemoteAccessSection toast={toast} />
        </Accordion>

        <Accordion
          title={tr("卫星通道 Satellites（光谱电脑）", "Satellite channels (spectrometer PC)")}
          defaultOpen={false}
        >
          <SatelliteSettingsSection settings={settings} saving={saveSettings.isPending} save={save} />
        </Accordion>

        <Accordion title={tr("实验默认参数 Experiment defaults", "Experiment defaults")} defaultOpen>
          <ExperimentDefaultsSection settings={settings} saving={saveSettings.isPending} save={save} />
        </Accordion>

        <Accordion
          title={tr(
            "Z 参数组 Z-control presets（AI 按名应用，数值由代码写）",
            "Z-control presets (applied by name; values are written by code)",
          )}
          defaultOpen={false}
        >
          <ZCtrlPresetsSection saving={saveSettings.isPending} save={save} />
        </Accordion>

        <Accordion
          title={tr(
            "扫描参数档位 Scan policy（按尺度：速度 / 像素 / 反馈）",
            "Scan policy (by scale: speed / pixels / feedback)",
          )}
          defaultOpen={false}
        >
          <ScanPolicySection saving={saveSettings.isPending} save={save} />
        </Accordion>

        <Accordion
          title={tr(
            "仪器进退针 Instrument (退针方向 / dI/dV)",
            "Instrument approach / retract (retract direction / dI/dV)",
          )}
          defaultOpen={false}
        >
          <InstrumentProfileSection settings={settings} saving={saveSettings.isPending} save={save} />
        </Accordion>

        {legacy && (
          <Accordion title={tr("语音 Voice", "Voice")} defaultOpen={false}>
            <VoiceSection voices={voices} settings={settings} saving={saveSettings.isPending} save={save} />
          </Accordion>
        )}

        {legacy && (
          <Accordion title={tr("自治与成本 Autonomy & cost", "Autonomy & cost")} defaultOpen={false}>
            <AutonomySection settings={settings} saving={saveSettings.isPending} save={save} />
          </Accordion>
        )}

        {legacy && (
          <Accordion title={CONDUCT_SETTINGS_TITLE} defaultOpen={false}>
            <ConductSettingsSection settings={settings} saving={saveSettings.isPending} save={save} />
          </Accordion>
        )}

        <Accordion
          title={tr("对话处理与归档 Conversation & archiving", "Conversation & archiving")}
          defaultOpen={false}
        >
          <ConversationArchivingSection
            settings={settings}
            saving={saveSettings.isPending}
            save={save}
            legacy={legacy}
          />
        </Accordion>

        {legacy && (
          <Accordion title={tr("视觉判别阈值 Vision", "Vision thresholds")} defaultOpen={false}>
            <VisionThresholdsSection settings={settings} saving={saveSettings.isPending} save={save} />
          </Accordion>
        )}

        <Accordion
          title={tr(
            "经典针尖阈值 Classical（免模型 · 按仪器重标定）",
            "Classical tip thresholds (model-free · recalibrate per instrument)",
          )}
          defaultOpen={false}
        >
          <ClassicalThresholdsSection settings={settings} saving={saveSettings.isPending} save={save} />
        </Accordion>

        <Accordion
          title={tr(
            "电流监控 Current Monitor（常开 / 保留策略 / 告警阈值）",
            "Current monitor (always-on / retention / alert thresholds)",
          )}
          defaultOpen={false}
        >
          <div className="mb-3 max-w-2xl rounded-mast-card border border-mast-warn-border bg-mast-warn-bg p-3 text-xs text-mast-warn">
            {tr(
              "自动告警现在默认关闭，属于计划中的后续功能（默认阈值尚未针对通用场景标定）。电流的采集与记录仍然开启。硬件安全看门狗是独立的：电流饱和时它会自动退针，不受此处任何设置影响，始终有效。",
              "Automatic alerts are now off by default and are a planned future feature (the default thresholds are not yet calibrated for general use). Current acquisition and logging stay on. The hardware safety watchdog, which withdraws the tip when the current saturates, is independent of these settings and always stays active.",
            )}
          </div>
          <MonitoringSettingsSection settings={settings} saving={saveSettings.isPending} save={save} />
        </Accordion>

        <Accordion
          title={tr(
            "环境历史 Environment History（记录粒度 / 保留期 / 噪声谱）",
            "Environment history (logging interval / retention / noise spectrum)",
          )}
          defaultOpen={false}
        >
          <EnvHistorySettingsSection settings={settings} saving={saveSettings.isPending} save={save} />
        </Accordion>

        {/* The hardware-module switches live under Advanced -> System / Global ->
            Capability toggles. Switching a module on hands the agent DANGEROUS skills
            (a laser, an RF amplifier, probes that can collide), which is a different
            class of action from changing a font size. A signpost stays here so the
            control does not silently disappear. */}
        <Accordion title={tr("硬件模块 Hardware", "Hardware modules")} defaultOpen={false}>
          <HardwareModulesMoved hwModules={hwModules} />
        </Accordion>

        <Accordion title={tr("其它 Other", "Other")} defaultOpen={false}>
          <OtherSection />
        </Accordion>

        {legacy && (
          <Accordion title={tr("模型能力表 Capabilities", "Model capabilities")} defaultOpen={false}>
            <CapabilitiesSection models={models} />
          </Accordion>
        )}
      </Section>
      {toastNode}
    </div>
  );
}

// ── Admin PIN guidance ─────────────────────────────────────────────────────────
// Some settings are written only with the admin PIN (api/admin_pin.py GUARDED_KEYS:
// advanced_capabilities, hardware_modules, coarse_drive, tip_conditioning_overrides,
// station_profile),
// and the whole Advanced tab is PIN-gated. With no PIN set those keys are refused
// (fail-closed), which looks like "the setting will not save" unless this is explained.
function AdminPinGuideCard() {
  return (
    <div
      id={PIN_GUIDE_ANCHOR}
      className="mb-4 rounded-mast-card border border-mast-border bg-mast-panel-2 px-4 py-3 text-sm text-mast-muted"
    >
      <p className="font-semibold text-mast-text">
        {tr("为什么有些设置保存不了？管理员 PIN 说明", "Why won't some settings save? About the admin PIN")}
      </p>
      <p className="mt-1 text-xs">
        {tr(
          "部分设置受管理员 PIN 保护：高级能力开关、硬件模块、粗动驱动声明、修针方案覆写、站点画像（STM 站 / 光学站），以及整个“设置 → 高级”标签页。",
          "Some settings are protected by an admin PIN: advanced capabilities, hardware modules, the coarse-drive declaration, tip-conditioning overrides, the station profile (STM station / optical station), and the whole Settings → Advanced tab.",
        )}
      </p>
      <ul className="mt-2 list-disc space-y-1 pl-5 text-xs">
        <li>
          {tr(
            "在桌面启动器里点“设置高级管理密码”按钮来设置 PIN。PIN 只保存在本机，以哈希形式写入 config/admin_pin.txt，原文不落盘。",
            "Set the PIN in the desktop launcher with the “Set admin password” button. It is stored only on this machine, as a hash in config/admin_pin.txt; the PIN itself is never saved.",
          )}
        </li>
        <li>
          {tr(
            "没有默认 PIN。在设置之前，这些设置一律改不了（不是“可随意修改”，而是被拒绝）。",
            "There is no default PIN. Until one is set, these settings cannot be changed (they are refused, not left open).",
          )}
        </li>
        <li>
          {tr(
            "也可以在浏览器里设置：如果还没有 PIN，“设置 → 高级 → 能力开关”会显示“先设置一个管理 PIN”卡片。",
            "You can also set it in the browser: when no PIN exists, Settings → Advanced → capability toggles shows a “set a PIN first” card.",
          )}
        </li>
        <li>
          {tr(
            "需要 PIN 的保存操作，会在保存按钮旁（站点画像是在确认对话框里）出现 PIN 输入框；PIN 只随这一次保存发送，不会被存下来。",
            "Where a save needs the PIN, a PIN field appears next to the save button (for the station profile, in the confirmation dialog). The PIN is sent only with that save and is not kept.",
          )}
        </li>
        <li>
          {tr(
            "忘记了 PIN：先停止 MAST，删除安装目录里的 config/admin_pin.txt，再重新设置一个新的。",
            "Forgot the PIN: stop MAST, delete config/admin_pin.txt in the install folder, then set a new one.",
          )}
        </li>
      </ul>
    </div>
  );
}

// ── Legacy features switch ─────────────────────────────────────────────────────
function LegacyFeaturesCard() {
  const { enabled, loaded, setEnabled } = useLegacyFeatures();
  const err = setEnabled.error as Error | null;

  return (
    <div
      id={LEGACY_ANCHOR}
      className="mb-4 rounded-mast-card border border-mast-border bg-mast-panel-2 px-4 py-3"
    >
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-mast-text">
            {tr("旧版功能 / Legacy features", "Legacy features")}
          </p>
          <p className="mt-1 text-xs text-mast-muted">
            {tr(
              "旧版功能包括：内置 Agent 对话、Agents 页、查询助手、多日 conduct、记忆、视觉模型、对话反馈、愿望单反馈、用量与成本。默认关闭，相关页面和设置项会被隐藏。",
              "Legacy features cover: built-in agent chat, Agents, the query assistant, multi-day conduct, memory, the vision model, conversation feedback, wishlist feedback, and usage & cost. They are off by default and their pages and settings are hidden.",
            )}
          </p>
        </div>
        <div className={setEnabled.isPending || !loaded ? "pointer-events-none opacity-50" : ""}>
          <Toggle
            checked={enabled}
            onChange={(v) => setEnabled.mutate(v)}
            label={tr("启用旧版功能", "Enable legacy features")}
          />
        </div>
      </div>
      <p className="mt-2 text-xs text-mast-muted">
        {tr(
          `MAST 现在的设计用法是通过 MAST 插件，从 Claude Code 里控制（推荐使用 ${RECOMMENDED_MODEL_LABEL}）。`,
          `MAST is now meant to be controlled from Claude Code through the MAST plugin (we recommend ${RECOMMENDED_MODEL_LABEL}).`,
        )}
      </p>
      <p className="mt-1 text-xs text-mast-muted">
        {tr(
          "只有开启旧版功能时才会加载视觉模型；该开关需要重启 MAST 服务后生效。",
          "The vision model is only loaded when legacy features are on; this takes effect after restarting the MAST service.",
        )}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => openOnboardingGuide()}
          className="inline-flex items-center rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-3.5 py-1.5 text-sm hover:bg-mast-panel-2"
        >
          {tr("打开入门指南", "Open getting-started guide")}
        </button>
        {setEnabled.isPending && (
          <span className="text-xs text-mast-muted">{tr("保存中…", "Saving…")}</span>
        )}
        {setEnabled.isError && (
          <span className="text-xs text-mast-danger">
            {tr("保存失败：", "Save failed: ")}
            {String(err?.message ?? err)}
          </span>
        )}
        {setEnabled.isSuccess && !setEnabled.isPending && (
          <span className="text-xs text-mast-muted">
            {tr("已保存。视觉模型的加载变化需重启 MAST 服务。", "Saved. Restart the MAST service for the vision-model change to apply.")}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Station profile ────────────────────────────────────────────────────────────
// Whether this MAST service runs as an STM station or as an optical station (the
// WITec Raman computer, no STM). Unlike the legacy-features switch, a click here does
// NOT save: the profile is an admin-PIN setting (backend admin_pin.GUARDED_KEYS) that
// decides at the next start whether Nanonis is dialled and which watchdogs run, so the
// click opens a confirmation (StationProfileDialog) that says what each profile means
// and takes the PIN, and only that dialog's confirm button writes. Part of the profile
// is read live and part is decided once at start-up (the Nanonis connection and its
// threads), so a change is only complete after the service restarts: the card always
// says so, and turns amber while a saved change is still waiting for that restart. The
// values are identity strings; only the labels are translated.
const STATION_CHOICES: { value: StationProfile; label: string }[] = [
  { value: "stm", label: profileLabel("stm") },
  { value: "optical", label: profileLabel("optical") },
];

function StationProfileCard() {
  const { profile, configured, source, restartPending, loaded, setProfile } = useStationProfile();
  // The choice waiting for the operator's confirmation. Nothing is saved while it is set;
  // the radio shows it, the text of the card still says what is saved and what is in effect.
  const [pending, setPending] = useState<StationProfile | null>(null);
  const err = setProfile.error as Error | null;
  const busy = setProfile.isPending;
  const shown: StationProfile = pending ?? configured;

  // A click only asks. Choosing the saved profile again withdraws the question.
  const choose = (v: StationProfile) => {
    setProfile.reset();
    setPending(v === configured ? null : v);
  };
  // The one place that writes: the dialog's confirm button, with the PIN typed into it.
  const confirm = (pin: string) => {
    if (pending === null) return;
    setProfile.mutate({ profile: pending, pin }, { onSuccess: () => setPending(null) });
  };
  const cancel = () => {
    if (busy) return;
    setPending(null);
    setProfile.reset();
  };

  return (
    <div
      id={STATION_ANCHOR}
      className="mb-4 rounded-mast-card border border-mast-border bg-mast-panel-2 px-4 py-3"
    >
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-mast-text">
            {tr("站点画像 / Station profile", "Station profile")}
          </p>
          <p className="mt-1 text-xs text-mast-muted">{profileBlurb(shown)}</p>
        </div>
        <div className={busy || !loaded ? "pointer-events-none opacity-50" : ""}>
          <RadioGroup value={shown} onChange={choose} options={STATION_CHOICES} />
        </div>
      </div>
      <p className="mt-2 text-xs text-mast-muted">
        {tr("当前生效：", "In effect: ")}
        <span className="font-semibold text-mast-text">{loaded ? profileLabel(profile) : "—"}</span>
      </p>
      {source === "env" && (
        <p className="mt-2 text-xs text-mast-warn" role="status">
          {tr(
            "这台机器的站点画像由环境变量 MAST_STATION_PROFILE 指定：在去掉该变量之前，这里的选择不会改变实际运行的画像。",
            "This machine's station profile is pinned by the MAST_STATION_PROFILE environment variable: until that variable is removed, the choice here does not change the profile that runs.",
          )}
        </p>
      )}
      {restartPending && (
        <p
          className="mt-2 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-xs text-mast-warn"
          role="status"
        >
          {tr("已保存，需要重启 MAST 服务后生效。", "Saved. Restart the MAST service for it to take effect.")}
        </p>
      )}
      <p className="mt-2 text-xs text-mast-muted">
        {tr(
          "切换站点画像需要管理 PIN，并且要重启 MAST 服务后才完全生效（Nanonis 连接与线程在启动时决定）。",
          "Switching the station profile needs the admin PIN and only fully takes effect after restarting the MAST service (the Nanonis connection and its threads are decided at start-up).",
        )}
      </p>
      {setProfile.isSuccess && !restartPending && (
        <p className="mt-1 text-xs text-mast-muted">{tr("已保存。", "Saved.")}</p>
      )}
      <StationProfileDialog
        open={pending !== null}
        from={configured}
        to={pending ?? configured}
        source={source}
        busy={busy}
        error={setProfile.isError ? String(err?.message ?? err) : null}
        onConfirm={confirm}
        onClose={cancel}
      />
    </div>
  );
}

// ── Model ──────────────────────────────────────────────────────────────────────
/** Manual entry to the new-instrument setup checklist, plus its completion status.
 *
 *  "Remind again" clears only the completion stamp and changes no value; it just
 *  brings the banner back. The required-field check never looks at the stamp (it is
 *  computed from the values themselves), so clearing it neither relaxes anything
 *  nor pretends something is unfilled. */
function SetupEntryRow({ toast }: { toast: (t: string, tone?: "ok" | "err") => void }) {
  const qc = useQueryClient();
  const q = useInstrumentInit();
  const reopen = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/instrument-init/reopen", {});
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: INSTRUMENT_INIT_KEY });
      toast(tr("已重新打开：初始化提醒横幅会回来。", "Reopened: the setup reminder banner will come back."));
    },
    onError: () => toast(tr("操作失败。", "Operation failed."), "err"),
  });

  const d = q.data;
  const req = d?.counts?.required ?? { total: 0, complete: 0 };
  const done = !d?.needs_setup;

  return (
    <div className="mb-4 flex flex-wrap items-center gap-3 rounded-mast-card border border-mast-border bg-mast-panel-2 px-4 py-3">
      <div className="min-w-0">
        <p className="text-sm font-semibold text-mast-text">{tr("新仪器初始化", "New-instrument setup")}</p>
        <p className="mt-0.5 text-xs text-mast-muted">
          {tr(
            "「装到一台新机器上，还差哪些数」的那份清单：前放倍数 / 偏压极性 / 安全包络 / 进针参数组 / 进针撞不撞 / 有没有 XY 位移 / 修针破坏范围 …",
            "The checklist of what is still missing on a newly installed machine: preamp gain, bias polarity, safety envelope, approach preset, whether approach crashes the tip, whether XY coarse motion exists, tip-conditioning damage range …",
          )}
          {d && (
            <span className={done ? "ml-1 text-mast-auto" : "ml-1 text-mast-danger"}>
              {tr(`当前必填 ${req.complete}/${req.total}。`, `Required fields: ${req.complete}/${req.total}.`)}
            </span>
          )}
        </p>
      </div>
      <div className="ml-auto flex gap-2">
        <Link
          to="/settings/setup"
          className="inline-flex items-center rounded-mast-ctl bg-mast-accent px-3.5 py-2 text-sm font-semibold text-mast-accent-ink hover:opacity-90"
        >
          {tr("打开初始化页面", "Open setup page")}
        </Link>
        {d?.completed_at ? (
          <button
            type="button"
            disabled={reopen.isPending}
            onClick={() => reopen.mutate()}
            className="inline-flex items-center rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-3.5 py-2 text-sm hover:bg-mast-panel-2 disabled:opacity-50"
            title={tr(
              "清掉完成戳，让提醒横幅重新出现。不会改动任何数值。",
              "Clear the completion stamp so the reminder banner shows again. No values are changed.",
            )}
          >
            {tr("重新提醒", "Remind again")}
          </button>
        ) : null}
      </div>
    </div>
  );
}

function ModelSection({
  models,
  settings,
  saving,
  save,
}: {
  models: ReturnType<typeof useModels>;
  settings: ReturnType<typeof useSettings>;
  saving: boolean;
  save: (patch: SettingsPatch) => void;
}) {
  if (models.isPending || settings.isPending) return <Spinner />;
  if (models.isError) return <ErrorNote error={models.error} />;
  if (settings.isError) return <ErrorNote error={settings.error} />;
  if (!models.data?.models?.length) return <EmptyNote label={tr("无可用模型", "No models available")} />;

  const s: Partial<NonNullable<typeof settings.data>> = settings.data ?? {};
  const modelList = models.data.models;

  const currentAlias = s.model_alias ?? models.data.default_alias;
  const currentModel = modelList.find((m) => m.alias === currentAlias);
  // Thinking is locked, not selectable: the system pins every model to its strongest
  // supported value. Only the actual locked value is displayed, derived from the
  // model's thinking_mode (see thinkingDisplay), consistently for the global model,
  // the query assistant and each agent.
  const thinkingMode = currentModel?.thinking_mode ?? "tunable";
  const thinkInfo = thinkingDisplay(thinkingMode);
  const currentQa = s.qa_model ?? "kimi-k2.6";
  // The query-assistant thinking mirrors the selected qa_model's locked value.
  const qaThinkingMode = modelList.find((m) => m.alias === currentQa)?.thinking_mode ?? "tunable";
  const qaThinkInfo = thinkingDisplay(qaThinkingMode);

  return (
    <div className="space-y-4">
      {/* Model info: badge, description and the Thinking line */}
      {currentModel && (
        <div className="rounded-md border border-mast-border bg-mast-bg/40 p-3 text-sm">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="rounded bg-mast-accent/20 px-2 py-0.5 font-semibold text-mast-accent">
              {currentModel.alias}
            </span>
            <span className="text-mast-muted">{currentModel.description}</span>
          </div>
          <div className="mt-1 flex items-center gap-1.5 text-mast-muted">
            <span>Thinking:</span>
            <Badge tone={thinkInfo.tone}>{thinkInfo.label}</Badge>
          </div>
        </div>
      )}

      <Field label={tr("全局对话模型 Global model", "Global chat model")}>
        <SelectField
          value={currentAlias}
          onChange={(v) => save({ model_alias: v })}
          options={modelList.map((m) => ({ value: m.alias, label: `${m.alias}  (${m.provider})` }))}
        />
      </Field>

      {/* Thinking: read-only display of the value actually in effect (not selectable),
          matching the model info card and the per-agent rows. */}
      <Field
        label="Thinking"
        hint={tr("思考强度由系统锁定为最强，不可手动选择", "Thinking effort is locked to the strongest level by the system and cannot be chosen")}
      >
        <span className="inline-flex items-center gap-1.5 rounded-md border border-mast-border bg-mast-bg/40 px-2 py-1.5">
          <Badge tone={thinkInfo.tone}>{thinkInfo.label}</Badge>
        </span>
      </Field>

      <Field label={tr("查询助手模型 QA helper model", "Query assistant model")}>
        <SelectField value={currentQa} onChange={(v) => save({ qa_model: v })} options={QA_MODEL_CHOICES} />
      </Field>

      {/* Query-assistant Thinking: read-only display of the qa_model's locked value,
          styled like the global Thinking display above. */}
      <Field
        label={tr("查询助手 Thinking", "Query assistant Thinking")}
        hint={tr("思考强度由系统锁定，仅显示实际生效值", "Thinking effort is locked by the system; only the effective value is shown")}
      >
        <span className="inline-flex items-center gap-1.5 rounded-md border border-mast-border bg-mast-bg/40 px-2 py-1.5">
          <Badge tone={qaThinkInfo.tone}>{qaThinkInfo.label}</Badge>
        </span>
      </Field>

      {saving && <p className="text-xs text-mast-muted">{tr("保存中…", "Saving…")}</p>}
    </div>
  );
}

// ── Appearance ─────────────────────────────────────────────────────────────────
function AppearanceSection({
  settings,
  saving,
  save,
  legacy,
}: {
  settings: ReturnType<typeof useSettings>;
  saving: boolean;
  save: (patch: SettingsPatch) => void;
  legacy: boolean;
}) {
  // Theme: reflect and drive the same zustand store the top-bar toggle uses, so this
  // control applies live (AppLayout's effect mirrors store.theme to <html>) and stays
  // in sync with the header toggle. The Light/Dark string is persisted too.
  const storeTheme = useUiStore((st) => st.theme);
  const setStoreTheme = useUiStore((st) => st.setTheme);

  const s: Partial<NonNullable<typeof settings.data>> = settings.data ?? {};
  const font = s.font_scale && ["小", "中", "大"].includes(s.font_scale) ? s.font_scale : "中";
  // The radio reflects the live store theme (not just the persisted value), so a
  // top-bar toggle is mirrored here immediately.
  const theme = storeThemeToSettings(storeTheme);
  const codexLive = s.codex_live_search ?? true;

  // On settings load, apply the persisted font live.
  useEffect(() => {
    if (settings.data) applyFontScale(font);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings.data, font]);

  if (settings.isPending) return <Spinner />;
  if (settings.isError) return <ErrorNote error={settings.error} />;

  const onTheme = (v: string) => {
    setStoreTheme(settingsThemeToStore(v)); // live-apply via AppLayout effect
    save({ theme: v }); // persist Light/Dark
  };
  const onFont = (v: string) => {
    applyFontScale(v); // live-apply (no reload)
    save({ font_scale: v }); // persist
  };

  return (
    <div className="space-y-4">
      <Field label={tr("字体大小 Font", "Font size")}>
        <RadioGroup value={font} onChange={onFont} options={FONT_CHOICES} />
      </Field>
      <Field label={tr("主题 Theme", "Theme")}>
        <RadioGroup value={theme} onChange={onTheme} options={THEME_CHOICES} />
      </Field>
      {legacy && (
        <Checkbox
          checked={!!codexLive}
          onChange={(v) => save({ codex_live_search: v })}
          label={tr("技能 Codex 实时搜索 Live skill search", "Codex live skill search")}
        />
      )}
      {saving && <p className="text-xs text-mast-muted">{tr("保存中…", "Saving…")}</p>}
    </div>
  );
}

// ── Voice ──────────────────────────────────────────────────────────────────────
function VoiceSection({
  voices,
  settings,
  saving,
  save,
}: {
  voices: ReturnType<typeof useVoices>;
  settings: ReturnType<typeof useSettings>;
  saving: boolean;
  save: (patch: SettingsPatch) => void;
}) {
  if (voices.isPending || settings.isPending) return <Spinner />;
  if (settings.isError) return <ErrorNote error={settings.error} />;
  if (voices.isError) return <ErrorNote error={voices.error} />;
  if (voices.data?.degraded) return <DegradedNote what={tr("语音", "Voice")} />;

  const s: Partial<NonNullable<typeof settings.data>> = settings.data ?? {};
  const enabled = !!voices.data?.enabled;
  const voiceOptions = [
    { value: "Off", label: "Off" },
    ...(voices.data?.voices ?? []).map((v) => ({ value: v, label: v })),
  ];
  const currentVoice = s.voice && voiceOptions.some((o) => o.value === s.voice) ? s.voice : "Off";
  const autoplay = !!s.voice_autoplay;
  const mode = s.voice_mode ?? "ptt";
  const narrate = s.voice_narrate ?? true;

  return (
    <div className="space-y-4">
      {!enabled && (
        <p className="rounded-md border border-mast-warn-border bg-mast-warn-bg p-2 text-xs text-mast-warn">
          {tr(
            "语音子系统未就绪（缺少 DashScope 密钥）。设置仍会持久化，配置密钥后生效。",
            "The voice subsystem is not ready (missing DashScope key). Settings are still saved and take effect once a key is configured.",
          )}
        </p>
      )}
      <Field label={tr("TTS 音色 Voice", "TTS voice")}>
        <SelectField value={currentVoice} onChange={(v) => save({ voice: v })} options={voiceOptions} />
      </Field>
      <Field label={tr("默认语音模式 Mode", "Default voice mode")}>
        <SelectField
          value={mode}
          onChange={(v) => save({ voice_mode: v })}
          options={[
            { value: "ptt", label: tr("按住说话 PTT", "Push to talk (PTT)") },
            { value: "wake", label: tr("唤醒词 Wake（说「MAST」）", "Wake word (say “MAST”)") },
            { value: "duplex", label: tr("全双工 Duplex（连续对话）", "Full duplex (continuous conversation)") },
          ]}
        />
      </Field>
      <div className="flex items-center justify-between">
        <span className="text-sm">{tr("播报执行过程 Narrate（如「扫描完成」）", "Narrate progress (e.g. “scan complete”)")}</span>
        <Toggle checked={narrate} onChange={(v) => save({ voice_narrate: v })} label={tr("播报执行过程", "Narrate progress")} />
      </div>
      <div className="flex items-center justify-between">
        <span className="text-sm">{tr("自动朗读助手回复 Autoplay", "Autoplay assistant replies")}</span>
        <Toggle checked={autoplay} onChange={(v) => save({ voice_autoplay: v })} label={tr("自动朗读助手回复 Autoplay", "Autoplay assistant replies")} />
      </div>
      <p className="text-xs text-mast-faint">
        {tr(
          "语音走流式 realtime（qwen3-*-flash-realtime），不可用时自动回退批量。全双工/唤醒用本地能量 VAD 断句与打断。",
          "Voice uses streaming realtime (qwen3-*-flash-realtime) and falls back to batch when unavailable. Full-duplex and wake modes use a local energy VAD for utterance segmentation and barge-in.",
        )}
      </p>
      {saving && <p className="text-xs text-mast-muted">{tr("保存中…", "Saving…")}</p>}
    </div>
  );
}

// ── Autonomy ───────────────────────────────────────────────────────────────────
// One numeric setting row: local text state, commit on blur/Enter, empty = use default.
// Text (not <input type="number">) so a half-typed value never round-trips through
// Number() and rewrites itself under the cursor.
function NumericSettingRow({
  label,
  hint,
  placeholder,
  value,
  saving,
  onCommit,
  parse,
}: {
  label: string;
  hint: string;
  placeholder: string;
  value: number | null;
  saving: boolean;
  onCommit: (v: number) => void;
  parse: (raw: string) => number | null;
}) {
  const [val, setVal] = useState<string>(value == null ? "" : String(value));
  useEffect(() => {
    setVal(value == null ? "" : String(value));
  }, [value]);

  const commit = () => {
    const t = val.trim();
    if (t === "") {
      // Blank means "leave unchanged" (the backend default applies), not "set to 0":
      // for a ceiling those two mean opposite things, and a blank box must never
      // silently disable a guard.
      setVal(value == null ? "" : String(value));
      return;
    }
    const parsed = parse(t);
    if (parsed == null) {
      setVal(value == null ? "" : String(value));
      return;
    }
    if (parsed !== value) onCommit(parsed);
    setVal(String(parsed));
  };

  return (
    <Field label={label} hint={hint}>
      <input
        type="text"
        inputMode="decimal"
        value={val}
        placeholder={placeholder}
        disabled={saving}
        onChange={(e) => setVal(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.currentTarget as HTMLInputElement).blur();
        }}
        className="w-32 rounded border border-mast-border bg-mast-bg px-2 py-1 font-mono text-sm tabular-nums text-mast-text"
      />
    </Field>
  );
}

function AutonomySection({
  settings,
  saving,
  save,
}: {
  settings: ReturnType<typeof useSettings>;
  saving: boolean;
  save: (patch: SettingsPatch) => void;
}) {
  const recursion = settings.data?.orchestrator_recursion_limit ?? null;
  const runBudget = settings.data?.orchestrator_run_budget_usd ?? null;
  const dailyBudget = settings.data?.daily_budget_usd ?? null;
  const wakeMax = settings.data?.wake_max_per_day ?? null;

  return (
    <div className="max-w-md space-y-4">
      <NumericSettingRow
        label={tr("任务步数上限 recursion_limit", "Task step limit (recursion_limit)")}
        hint={tr(
          "单次自治任务（run-task）的 super-step 预算，默认 250（下限 50）。越大越能跑完长 campaign；真正空转由跳数熔断和成本闸门更早拦截，这只是最终兜底。留空 = 用默认。",
          "Super-step budget of one autonomous task (run-task); default 250 (minimum 50). Larger values let long campaigns finish; genuine idle loops are caught earlier by the hop breaker and the cost gates, so this is only the last backstop. Blank = use the default.",
        )}
        placeholder="250"
        value={recursion}
        saving={saving}
        parse={(raw) => {
          const n = Number.parseInt(raw, 10);
          return Number.isNaN(n) ? null : Math.max(50, n);
        }}
        onCommit={(v) => save({ orchestrator_recursion_limit: v })}
      />

      {/* Cost gates. The two gates measure different things; neither replaces the other:
          - the per-run cap stops a single run that runs away;
          - the daily cap bounds the number of runs: a woken agent starts a fresh run with
            a fresh per-run budget, so the per-run cap cannot see it.
          A runaway of identical successful loops is possible; idle-loop
          detection keys on repeated failures and cannot see that shape. */}
      <NumericSettingRow
        label={tr("单次任务成本上限 (USD)", "Per-task cost limit (USD)")}
        hint={tr(
          "一次自治任务最多花多少钱，超了就停。默认 80；填 0 = 关闭这道闸门（不是 0 元）。这是兜底不是配额——正当的多阶段流水线花费通常远低于它，把它压得太低反而会把长 campaign 中途掐断。计量口径：账本里没有 run 编号，只能按时间窗算，所以并发的别的调用会被算进来（偏高，安全方向）。",
          "The most one autonomous task may spend before it is stopped. Default 80; 0 = disable this gate (not “zero dollars”). This is a backstop, not a quota: a legitimate multi-stage pipeline typically costs far less than it, and setting it too low would cut long campaigns off midway. Metering: the ledger has no run id, so cost is summed over a time window and concurrent calls are included (over-counts, which is the safe direction).",
        )}
        placeholder="80"
        value={runBudget}
        saving={saving}
        parse={(raw) => {
          const n = Number.parseFloat(raw);
          return Number.isNaN(n) ? null : Math.max(0, Math.round(n * 100) / 100);
        }}
        onCommit={(v) => save({ orchestrator_run_budget_usd: v })}
      />

      <NumericSettingRow
        label={tr("每日总成本上限 (USD)", "Daily total cost limit (USD)")}
        hint={tr(
          "今天（本地零点起）所有任务加起来最多花多少，超了就不再自动唤醒被搁置的 agent。默认 300；填 0 = 关闭。单次上限看不见「任务数量」，而自动唤醒放大的正是数量——十次唤醒各自都合法，加起来才是问题。",
          "The most all tasks together may spend today (from local midnight); beyond it, parked agents are no longer woken automatically. Default 300; 0 = disable. The per-task limit cannot see the number of tasks, and automatic wake-ups multiply exactly that: ten wake-ups can each be legitimate while the total is the problem.",
        )}
        placeholder="300"
        value={dailyBudget}
        saving={saving}
        parse={(raw) => {
          const n = Number.parseFloat(raw);
          return Number.isNaN(n) ? null : Math.max(0, Math.round(n * 100) / 100);
        }}
        onCommit={(v) => save({ daily_budget_usd: v })}
      />

      {/* Wake-count quota. Here 0 means the opposite of 0 in the two cost gates above:
          - cost 0 = no cap (0 is what an unset amount looks like);
          - count 0 = never (a user typing 0 into a safety counter means "do not do this").
          There is deliberately no "unlimited" option: this counter is the only bound on
          the artifact-driven wake loop. Every other gate is per-run, and each wake-up is
          a fresh run with all limits reset; every loop iteration also succeeds, and idle
          detection keys on repeated failures, so it cannot see such a loop. */}
      <NumericSettingRow
        label={tr("每个实验每天最多自动唤醒几次", "Max automatic wake-ups per experiment per day")}
        hint={tr(
          "被搁置的 agent 每天最多自动醒来几次（按实验分别计）。默认 6——高于任何正当流水线（六阶段最多一阶段醒一次），远低于「互相唤醒」的死循环。填 0 = 完全不自动唤醒（搁置的 agent 只会等你手动处理）。这是自动唤醒唯一的次数边界，所以不提供「不限次」。",
          "How many times per day a parked agent may be woken automatically (counted per experiment). Default 6: above any legitimate pipeline (a six-stage one wakes at most once per stage) and far below a mutual-wake loop. 0 = never wake automatically (parked agents wait for you). This is the only count bound on automatic wake-ups, so there is no “unlimited” option.",
        )}
        placeholder="6"
        value={wakeMax}
        saving={saving}
        parse={(raw) => {
          const n = Number.parseInt(raw, 10);
          // Clamp to >= 0: a negative value disables the check in the scheduler (a
          // test hook) and must not be reachable from the settings page.
          return Number.isNaN(n) ? null : Math.max(0, n);
        }}
        onCommit={(v) => save({ wake_max_per_day: v })}
      />

      {/* Per-call model / tool-call limits. These count model calls and tool calls per
          turn, which is a different quantity from the super-step count of
          recursion_limit above. A long autonomous flow (tip conditioning, scan,
          interpretation) can hit the default of 30 and is interrupted on the spot. */}
      <NumericSettingRow
        label={tr("单次对话的模型调用上限", "Model-call limit per turn")}
        hint={tr(
          "一轮里模型最多开口几次，默认 30。长的自治流程（修针→扫图→判读）很容易撞上它，撞上就当场中断。改完下次启动生效。不接受 0/负数：内核会把它换回 30。",
          "How many times the model may respond in one turn; default 30. Long autonomous flows (tip conditioning → scan → interpretation) easily hit it and are interrupted on the spot. Takes effect on next start. 0 and negative values are not accepted: the core would reset them to 30.",
        )}
        placeholder="30"
        value={settings.data?.chat_model_calls_per_run ?? null}
        saving={saving}
        parse={(raw) => {
          const n = Number.parseInt(raw, 10);
          return Number.isNaN(n) ? null : Math.max(1, n);
        }}
        onCommit={(v) => save({ chat_model_calls_per_run: v })}
      />

      <NumericSettingRow
        label={tr("单次对话的工具调用上限", "Tool-call limit per turn")}
        hint={tr(
          "同一轮里最多执行几个工具/技能，默认 80。改完下次启动生效。",
          "How many tools/skills may run in one turn; default 80. Takes effect on next start.",
        )}
        placeholder="80"
        value={settings.data?.chat_tool_calls_per_run ?? null}
        saving={saving}
        parse={(raw) => {
          const n = Number.parseInt(raw, 10);
          return Number.isNaN(n) ? null : Math.max(1, n);
        }}
        onCommit={(v) => save({ chat_tool_calls_per_run: v })}
      />

      {/* Cumulative per-thread limits. Here 0 means the opposite of the per-run limits
          above: a per-run 0 is reset to the factory default by the core (so the backend
          rejects it), while a cumulative 0 means unlimited (the factory state). */}
      <NumericSettingRow
        label={tr("跨会话累计的模型调用上限", "Cumulative model-call limit per thread")}
        hint={tr(
          "一条会话从建立起累计的模型调用上限，0 = 不限（默认）。累计上限不会自己重置，设小了会把一条长期会话彻底卡死——除非你确切知道为什么要开它，留 0。",
          "Cumulative model-call limit of one conversation thread since it was created; 0 = unlimited (default). A cumulative limit never resets by itself and a small value will wedge a long-lived thread; leave it at 0 unless you know exactly why you need it.",
        )}
        placeholder="0"
        value={settings.data?.chat_model_calls_per_thread ?? null}
        saving={saving}
        parse={(raw) => {
          const n = Number.parseInt(raw, 10);
          return Number.isNaN(n) ? null : Math.max(0, n);
        }}
        onCommit={(v) => save({ chat_model_calls_per_thread: v })}
      />

      <NumericSettingRow
        label={tr("跨会话累计的工具调用上限", "Cumulative tool-call limit per thread")}
        hint={tr("同上，0 = 不限（默认）。", "As above; 0 = unlimited (default).")}
        placeholder="0"
        value={settings.data?.chat_tool_calls_per_thread ?? null}
        saving={saving}
        parse={(raw) => {
          const n = Number.parseInt(raw, 10);
          return Number.isNaN(n) ? null : Math.max(0, n);
        }}
        onCommit={(v) => save({ chat_tool_calls_per_thread: v })}
      />
    </div>
  );
}

// ── Conversation handling and archiving ────────────────────────────────────────
// These switches are in SettingsStore.KNOWN_KEYS and read by the core; the write
// endpoint accepts them. A key that can be stored but has no UI is unreachable, so
// each one is exposed here. With legacy features off, only the experiment-archiving
// rows are shown; the agent-related rows are hidden.
function ConversationArchivingSection({
  settings,
  saving,
  save,
  legacy,
}: {
  settings: ReturnType<typeof useSettings>;
  saving: boolean;
  save: (patch: SettingsPatch) => void;
  legacy: boolean;
}) {
  const s = settings.data;
  // The factory default of every key in this group is "on", and an absent value
  // means "on", so `?? true` mirrors the backend's `v is None -> True`.
  const on = (v: boolean | null | undefined) => v ?? true;

  return (
    <div className="max-w-xl space-y-4">
      {legacy && (
        <>
          <div className="flex items-center justify-between">
            <span className="text-sm">
              {tr("工具返回精炼 Tool refine", "Tool-result refinement")}
              <span className="ml-2 text-xs text-mast-faint">
                {tr(
                  "压缩过长的工具返回再给模型看。关掉它模型读到的是原文。改完下次启动生效。",
                  "Condense overly long tool results before the model sees them. When off, the model reads the raw output. Takes effect on next start.",
                )}
              </span>
            </span>
            <Toggle
              checked={on(s?.tool_refine_enabled)}
              onChange={(v) => save({ tool_refine_enabled: v })}
              label={tr("工具返回精炼", "Tool-result refinement")}
            />
          </div>

          <NumericSettingRow
            label={tr("精炼阈值（字符）", "Refinement threshold (characters)")}
            hint={tr(
              "只精炼比这个长的工具返回，默认 600。想整个关掉请用上面的开关，不要填 0。",
              "Only tool results longer than this are refined; default 600. To turn refinement off entirely use the switch above, not 0.",
            )}
            placeholder="600"
            value={s?.tool_refine_min_chars ?? null}
            saving={saving}
            parse={(raw) => {
              const n = Number.parseInt(raw, 10);
              return Number.isNaN(n) ? null : Math.max(1, n);
            }}
            onCommit={(v) => save({ tool_refine_min_chars: v })}
          />

          <Field
            label={tr("压缩摘要模型 Compaction model", "Compaction summary model")}
            hint={tr(
              "对话压缩用哪个模型写摘要。留空 = 跟随当前 agent 的模型（默认，够用）。改完下次启动生效。",
              "Which model writes the summary when a conversation is compacted. Blank = follow the current agent's model (default, sufficient). Takes effect on next start.",
            )}
          >
            <TextCommitField
              value={s?.compaction_model ?? ""}
              saving={saving}
              placeholder={tr("（跟随当前模型）", "(follow current model)")}
              onCommit={(v) => save({ compaction_model: v })}
            />
          </Field>

          <div className="flex items-center justify-between">
            <span className="text-sm">
              {tr("全文到货后自动续跑 Literature auto-resume", "Auto-resume when the full text arrives")}
              <span className="ml-2 text-xs text-mast-faint">
                {tr(
                  "你上传了 agent 要的论文之后，自动把它停住的那一轮跑完。",
                  "After you upload the paper the agent asked for, the turn that was waiting for it is resumed automatically.",
                )}
              </span>
            </span>
            <Toggle
              checked={on(s?.literature_fetch_auto_resume)}
              onChange={(v) => save({ literature_fetch_auto_resume: v })}
              label={tr("全文到货后自动续跑", "Auto-resume when the full text arrives")}
            />
          </div>

          <div className="border-t border-mast-border pt-4" />
        </>
      )}

      <div className="flex items-center justify-between">
        <span className="text-sm">
          {tr("归档 Nanonis 产物 Ingest", "Archive Nanonis output (ingest)")}
          <span className="ml-2 text-xs text-mast-faint">
            {tr(
              "把测量文件复制进实验文件夹。总开关，改完下次启动生效。",
              "Copy measurement files into the experiment folder. Master switch; takes effect on next start.",
            )}
          </span>
        </span>
        <Toggle
          checked={on(s?.ingest_enabled)}
          onChange={(v) => save({ ingest_enabled: v })}
          label={tr("归档 Nanonis 产物", "Archive Nanonis output")}
        />
      </div>

      <div className="flex items-center justify-between">
        <span className="text-sm">
          {tr("后台兜底扫盘 Watcher", "Background fallback scan (watcher)")}
          <span className="ml-2 text-xs text-mast-faint">
            {tr(
              "把你手动存的文件也收进来（source=manual）。当场生效 —— 但前提是上面那个总开关在**本次启动时**是开着的：关着的话归档整条链根本没建起来，这里开了也不动。",
              "Also collects files you saved by hand (source=manual). Takes effect immediately, but only if the master switch above was on at this start: otherwise the archiving chain was never built and enabling this does nothing.",
            )}
          </span>
        </span>
        <Toggle
          checked={on(s?.ingest_watcher_enabled)}
          onChange={(v) => save({ ingest_watcher_enabled: v })}
          label={tr("后台兜底扫盘", "Background fallback scan")}
        />
      </div>

      <Field
        label={tr("归档方式 Copy mode", "Archive mode")}
        hint={tr(
          "hardlink 不占额外磁盘，但 Nanonis 保存目录和实验文件夹必须在同一个卷上；跨卷时只能用 copy。",
          "hardlink uses no extra disk, but the Nanonis save folder and the experiment folder must be on the same volume; across volumes only copy works.",
        )}
      >
        <SelectField
          value={(s?.ingest_copy_mode as "copy" | "hardlink") ?? "copy"}
          onChange={(v) => save({ ingest_copy_mode: v })}
          options={[
            { value: "copy", label: tr("copy — 复制一份（任何情况都行）", "copy — make a copy (always works)") },
            { value: "hardlink", label: tr("hardlink — 硬链接（省盘，仅限同卷）", "hardlink — hard link (saves disk, same volume only)") },
          ]}
        />
      </Field>

      <div className="flex items-center justify-between">
        <span className="text-sm">
          {tr("环境读数同时写 CSV", "Also write environment readings to CSV")}
          <span className="ml-2 text-xs text-mast-faint">
            {tr(
              "温度等读数额外落一份 CSV，供 Excel / Origin 直接打开。",
              "Temperature and other readings are additionally saved as CSV, ready to open in Excel / Origin.",
            )}
          </span>
        </span>
        <Toggle
          checked={on(s?.env_csv_enabled)}
          onChange={(v) => save({ env_csv_enabled: v })}
          label={tr("环境读数同时写 CSV", "Also write environment readings to CSV")}
        />
      </div>

      <div className="flex items-center justify-between">
        <span className="text-sm">
          {tr("对话增量导出 Conversation export", "Incremental conversation export")}
          <span className="ml-2 text-xs text-mast-faint">
            {tr(
              "导出成 jsonl + md。这是唯一能保住被 8000 行上限裁掉的那部分历史的地方。",
              "Exported as jsonl + md. This is the only place that keeps the history trimmed off by the 8000-line cap.",
            )}
          </span>
        </span>
        <Toggle
          checked={on(s?.conv_export_enabled)}
          onChange={(v) => save({ conv_export_enabled: v })}
          label={tr("对话增量导出", "Incremental conversation export")}
        />
      </div>
    </div>
  );
}

/** Text input that commits on blur/Enter (same rhythm as NumericSettingRow: no save per keystroke). */
function TextCommitField({
  value,
  saving,
  placeholder,
  onCommit,
}: {
  value: string;
  saving: boolean;
  placeholder?: string;
  onCommit: (v: string) => void;
}) {
  const [val, setVal] = useState(value);
  useEffect(() => {
    setVal(value);
  }, [value]);
  const commit = () => {
    const t = val.trim();
    if (t !== value) onCommit(t);
    setVal(t);
  };
  return (
    <input
      type="text"
      value={val}
      placeholder={placeholder}
      disabled={saving}
      onChange={(e) => setVal(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.currentTarget as HTMLInputElement).blur();
      }}
      className="w-full rounded border border-mast-border bg-mast-bg px-2 py-1 font-mono text-sm text-mast-text"
    />
  );
}

// ── Vision thresholds ──────────────────────────────────────────────────────────
// The tip-quality valves of the vision model. Model weights are fixed; these only tune
// how strict the good/bad and usable judgement is. The 6 knobs are the overall
// good/bad cut plus per-dimension cuts (Q/N/K/T/S). C (segmentation) has no pass/fail cut.
//
// `def` must match the VisionThresholds defaults in MASTv2/mast/vision/thresholds.py
// (6 numbers, kept in sync by hand). The backend is authoritative; `def` only seeds
// the input when nothing is persisted yet. Persisted values always win over `def`.
const VISION_THRESHOLD_KNOBS: {
  key: string; label: string; hint: string; min: number; max: number; step: string; def: number;
}[] = [
  {
    key: "coarse_good_threshold", label: tr("总门槛 好/坏切分", "Overall good/bad cut"), def: 0.35, min: 0.3, max: 0.7, step: "0.05",
    hint: tr(
      "融合分 ≥ 此值判「良好」。最关键——直接决定「针尖坏→处理」是否触发。调低 = 更宽松，更少触发针尖处理。（旧严值 0.5）",
      "Fused score ≥ this value is judged “good”. The most important knob: it decides whether “bad tip → process” fires. Lower = more lenient, fewer tip-processing triggers. (old strict value 0.5)",
    ),
  },
  {
    key: "m0_quality_min", label: tr("Q 质量下限", "Q quality minimum"), def: 62, min: 60, max: 85, step: "1",
    hint: tr(
      "质量分 ≥ 此值（且轴比达标）才算最优形貌 M0。调低 = 更多针尖算优质 / 可用。（旧严值 72）",
      "Quality score ≥ this value (and axis ratio passing) counts as the best morphology M0. Lower = more tips count as high-quality / usable. (old strict value 72)",
    ),
  },
  {
    key: "multi_apex_p_max", label: tr("N 多针尖容忍", "N multi-apex tolerance"), def: 0.7, min: 0.2, max: 0.8, step: "0.05",
    hint: tr(
      "多针尖概率 > 此值判为多针尖（不可用）。最强坏信号。调高 = 更能容忍疑似多针尖。（旧严值 0.5）",
      "Multi-apex probability > this value is judged multi-apex (unusable). The strongest bad signal. Higher = more tolerant of suspected multi-apex tips. (old strict value 0.5)",
    ),
  },
  {
    key: "contam_p_max", label: tr("K 污染容忍", "K contamination tolerance"), def: 0.7, min: 0.2, max: 0.8, step: "0.05",
    hint: tr(
      "污染概率 > 此值判为污染（不可用）。调高 = 更能容忍疑似污染。（旧严值 0.5）",
      "Contamination probability > this value is judged contaminated (unusable). Higher = more tolerant of suspected contamination. (old strict value 0.5)",
    ),
  },
  {
    key: "instability_p_max", label: tr("T 不稳定容忍", "T instability tolerance"), def: 0.7, min: 0.2, max: 0.8, step: "0.05",
    hint: tr(
      "不稳定概率 > 此值判为扰动（不可用）。调高 = 更能容忍疑似不稳定。（旧严值 0.5）",
      "Instability probability > this value is judged disturbed (unusable). Higher = more tolerant of suspected instability. (old strict value 0.5)",
    ),
  },
  {
    key: "m0_axis_ratio_min", label: tr("S 轴比下限", "S axis-ratio minimum"), def: 0.45, min: 0.4, max: 0.9, step: "0.05",
    hint: tr(
      "针尖轴比 ≥ 此值（且质量达标）才算最优形貌 M0。调低 = 更能接受略椭圆的针尖。（旧严值 0.6）",
      "Tip axis ratio ≥ this value (and quality passing) counts as the best morphology M0. Lower = more accepting of slightly elliptical tips. (old strict value 0.6)",
    ),
  },
];

// Per-instrument knobs for the model-free classical tip-quality tools
// (mast/vision/classical_thresholds.py). Pure algorithms, no model involved.
const CLASSICAL_THRESHOLD_KNOBS: {
  key: string; label: string; hint: string; min: number; max: number; step: string; def: number;
}[] = [
  {
    key: "tq_double_threshold", label: tr("双针尖判定阈值", "Double-tip threshold"), def: 0.18, min: 0.05, max: 1.0, step: "0.01",
    hint: tr(
      "自相关副本峰分 > 此值 → 判双/多针尖（坏）。默认 0.18 适合清晰特征图；杂乱纹理的真实图像基线偏高，需按目标仪器独立验证。",
      "Autocorrelation replica-peak score > this value → double/multi tip (bad). The default 0.18 suits images with clear features; real images with cluttered texture have a higher baseline and need independent validation on the target instrument.",
    ),
  },
  {
    key: "tq_instability_max", label: tr("正反扫描不稳容忍", "Trace/retrace instability tolerance"), def: 0.5, min: 0.1, max: 1.0, step: "0.05",
    hint: tr(
      "trace/retrace 移位容忍互相关不稳度 > 此值 → 判不稳定（坏）。阈值需按目标仪器独立验证。",
      "Trace/retrace shift-tolerant cross-correlation instability > this value → unstable (bad). The threshold needs independent validation on the target instrument.",
    ),
  },
  {
    key: "tq_sharpness_min", label: tr("FFT 锐度下限", "FFT sharpness minimum"), def: 8.0, min: 1.0, max: 100.0, step: "1",
    hint: tr(
      "FFT 锐度 < 此值且无晶格 → 判「无可分辨表面」（坏，堵住噪声→good）。调低 = 更宽松。",
      "FFT sharpness < this value with no lattice → “no resolvable surface” (bad; keeps noise from being judged good). Lower = more lenient.",
    ),
  },
  {
    key: "tq_change_threshold", label: tr("中途换针 t 阈值", "Mid-scan tip-change t threshold"), def: 6.0, min: 2.0, max: 30.0, step: "0.5",
    hint: tr(
      "逐行统计跳变 t 值 > 此值 → 判扫描中途换针（坏）。合成默认 6 在真实图像上可能偏敏感，需按目标仪器独立验证。",
      "Row-by-row statistical jump t value > this value → tip changed mid-scan (bad). The synthetic default of 6 may be too sensitive on real data; validate independently on the target instrument.",
    ),
  },
  {
    key: "oscillation_threshold", label: tr("反馈振荡峰比阈值", "Feedback-oscillation peak-ratio threshold"), def: 3.0, min: 1.5, max: 50.0, step: "0.5",
    hint: tr(
      "轴上/轴外 FFT 峰比 > 此值 → 判反馈振荡/振铃。调高 = 只报更强的条纹。",
      "On-axis / off-axis FFT peak ratio > this value → feedback oscillation / ringing. Higher = only stronger streaks are reported.",
    ),
  },
  {
    key: "iz_barrier_min", label: tr("I(z) 势垒下限 (eV)", "I(z) barrier minimum (eV)"), def: 0.5, min: 0.0, max: 5.0, step: "0.1",
    hint: tr(
      "I(z) 表观势垒低于此值不算「干净指数」。洁净金属针尖 ~4-5 eV。",
      "An I(z) apparent barrier below this value is not a “clean exponential”. A clean metal tip gives ~4-5 eV.",
    ),
  },
  {
    key: "iz_barrier_max", label: tr("I(z) 势垒上限 (eV)", "I(z) barrier maximum (eV)"), def: 8.0, min: 3.0, max: 15.0, step: "0.5",
    hint: tr(
      "I(z) 表观势垒高于此值不算「干净指数」（异常大多为拟合/噪声问题）。",
      "An I(z) apparent barrier above this value is not a “clean exponential” (an abnormally large value is usually a fit or noise problem).",
    ),
  },
];

const _fmtThreshold = (v: number) => String(Math.round(v * 1000) / 1000);

function ThresholdRow({
  label, hint, value, min, max, step, disabled, onCommit,
}: {
  label: string; hint: string; value: number; min: number; max: number;
  step: string; disabled: boolean; onCommit: (v: number) => void;
}) {
  const [text, setText] = useState(_fmtThreshold(value));
  useEffect(() => {
    setText(_fmtThreshold(value));
  }, [value]);

  const commit = () => {
    const t = text.trim();
    const n = Number.parseFloat(t);
    if (t === "" || Number.isNaN(n)) {
      setText(_fmtThreshold(value)); // revert junk
      return;
    }
    const clamped = Math.min(max, Math.max(min, n));
    setText(_fmtThreshold(clamped));
    if (clamped !== value) onCommit(clamped);
  };

  return (
    <Field label={label} hint={hint}>
      <input
        type="text"
        inputMode="decimal"
        value={text}
        step={step}
        disabled={disabled}
        placeholder={_fmtThreshold(value)}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.currentTarget as HTMLInputElement).blur();
        }}
        className="w-28 rounded border border-mast-border bg-mast-bg px-2 py-1 font-mono text-sm tabular-nums text-mast-text disabled:opacity-50"
      />
    </Field>
  );
}

// ── Experiment defaults ────────────────────────────────────────────────────────
// The operator's usual scan size / speed and the setpoint / bias they normally start
// from. These are preferences, not bounds: they are injected into the IC /
// experiment_design agents' context (via mast.agents._shared.experiment_prefs) as
// "prefer these defaults", and SafetyLimits still enforces the hard ranges. A blank
// field is simply not sent (the middleware skips it).
//
// The placeholder is shown when nothing is persisted; there is no baked-in default
// value (an unset preference must stay unset so the agent falls back to its own
// judgement; a scan size is never invented on the operator's behalf).
const EXPERIMENT_DEFAULT_FIELDS: {
  key: string; label: string; hint: string; min: number; max: number; step: string; placeholder: string;
}[] = [
  {
    key: "scan_size_nm", label: tr("扫描尺寸 (边长)", "Scan size (side length)"), placeholder: tr("如 50", "e.g. 50"), min: 0, max: 1e6, step: "1",
    hint: tr("常用扫描帧的边长，单位 nm。新扫描默认从这个尺寸起。", "Side length of the usual scan frame, in nm. New scans start from this size by default."),
  },
  {
    key: "scan_speed_nm_s", label: tr("扫描速度", "Scan speed"), placeholder: tr("如 20", "e.g. 20"), min: 0, max: 1e6, step: "1",
    hint: tr("常用扫描速度，单位 nm/s。", "Usual scan speed, in nm/s."),
  },
  {
    key: "line_time_s", label: tr("每线时间", "Time per line"), placeholder: tr("如 0.5", "e.g. 0.5"), min: 0, max: 1e4, step: "0.1",
    hint: tr("每条扫描线的时间，单位 s（扫描速度的另一种表述）。", "Time per scan line, in s (another way to state the scan speed)."),
  },
  {
    key: "scan_lines", label: tr("扫描线数 (分辨率)", "Scan lines (resolution)"), placeholder: tr("如 256", "e.g. 256"), min: 1, max: 8192, step: "1",
    hint: tr("每帧线数 / 像素分辨率。", "Lines per frame / pixel resolution."),
  },
  {
    key: "scan_angle_deg", label: tr("扫描旋转角", "Scan rotation angle"), placeholder: tr("如 0", "e.g. 0"), min: -360, max: 360, step: "1",
    hint: tr("扫描帧旋转角度，单位 °。", "Rotation angle of the scan frame, in °."),
  },
  {
    key: "setpoint_pa", label: tr("电流设定点 setpoint", "Current setpoint"), placeholder: tr("如 100", "e.g. 100"), min: 0, max: 1e8, step: "1",
    hint: tr("常用隧道电流设定点，单位 pA。", "Usual tunneling-current setpoint, in pA."),
  },
  {
    key: "bias_v", label: tr("偏压 bias", "Bias"), placeholder: tr("如 0.5", "e.g. 0.5"), min: -10, max: 10, step: "0.1",
    hint: tr("常用样品偏压，单位 V（安全范围 −10…+10 V）。", "Usual sample bias, in V (safe range −10…+10 V)."),
  },
];
const EXPERIMENT_NOTES_KEY = "notes";
// Enumerated preference: scan direction. "" = not set (removed from the dict).
const SCAN_DIRECTION_KEY = "scan_direction";
const SCAN_DIRECTION_CHOICES = [
  { value: "", label: tr("（未设定）", "(not set)") },
  { value: "up", label: tr("从下往上 (up)", "Bottom to top (up)") },
  { value: "down", label: tr("从上往下 (down)", "Top to bottom (down)") },
];

function ExperimentDefaultsSection({
  settings,
  saving,
  save,
}: {
  settings: ReturnType<typeof useSettings>;
  saving: boolean;
  save: (patch: SettingsPatch) => void;
}) {
  const persisted = (settings.data?.experiment_defaults ?? {}) as Record<string, unknown>;
  const numAt = (key: string): number | undefined =>
    typeof persisted[key] === "number" ? (persisted[key] as number) : undefined;
  const notes = typeof persisted[EXPERIMENT_NOTES_KEY] === "string"
    ? (persisted[EXPERIMENT_NOTES_KEY] as string)
    : "";
  const scanDir = typeof persisted[SCAN_DIRECTION_KEY] === "string"
    ? (persisted[SCAN_DIRECTION_KEY] as string)
    : "";

  // Whole-replace guard: SettingsStore replaces the experiment_defaults dict
  // wholesale (it does not merge), so a save must carry every currently-set field
  // plus the one change, otherwise the others would be wiped. Only fields that are
  // actually set are included (a blank preference stays unset).
  const buildBase = (): Record<string, unknown> => {
    const next: Record<string, unknown> = {};
    for (const f of EXPERIMENT_DEFAULT_FIELDS) {
      const v = numAt(f.key);
      if (typeof v === "number") next[f.key] = v;
    }
    if (scanDir) next[SCAN_DIRECTION_KEY] = scanDir;
    if (notes.trim()) next[EXPERIMENT_NOTES_KEY] = notes.trim();
    return next;
  };

  const commitNum = (key: string, value: number | null) => {
    const next = buildBase();
    if (value === null) delete next[key];
    else next[key] = value;
    save({ experiment_defaults: next });
  };

  const commitScanDir = (value: string) => {
    const next = buildBase();
    if (value) next[SCAN_DIRECTION_KEY] = value;
    else delete next[SCAN_DIRECTION_KEY];
    save({ experiment_defaults: next });
  };

  const commitNotes = (value: string) => {
    const next = buildBase();
    if (value.trim()) next[EXPERIMENT_NOTES_KEY] = value.trim();
    else delete next[EXPERIMENT_NOTES_KEY];
    save({ experiment_defaults: next });
  };

  // Empty dict clears every preference (the agent falls back to its own judgement).
  const resetDefaults = () => save({ experiment_defaults: {} });

  const anySet = Object.keys(buildBase()).length > 0;

  return (
    <div className="max-w-lg space-y-3">
      <p className="text-xs text-mast-muted">
        {trn(
          <>
            你惯用的实验起点：<b>扫多大、扫多快</b>、常用 setpoint / bias。设定后会作为<b>偏好</b>
            注入 IC / 实验设计 agent 的上下文——在你没有相反要求时，agent 优先采用它们。
            <b>它们是偏好，不是硬上限</b>，安全边界仍由 SafetyLimits 约束。留空的项不注入。
          </>,
          <>
            Your usual experiment starting point: <b>scan size and speed</b>, and the usual setpoint / bias. Once set,
            they are injected as <b>preferences</b> into the context of the IC / experiment-design agents, which use them
            unless you ask otherwise. <b>They are preferences, not hard limits</b>; safety bounds are still enforced by
            SafetyLimits. Blank items are not injected.
          </>,
        )}
      </p>
      {EXPERIMENT_DEFAULT_FIELDS.map((f) => (
        <ExperimentDefaultRow
          key={f.key}
          label={f.label}
          hint={f.hint}
          value={numAt(f.key)}
          min={f.min}
          max={f.max}
          step={f.step}
          placeholder={f.placeholder}
          disabled={saving}
          onCommit={(v) => commitNum(f.key, v)}
        />
      ))}
      <Field
        label={tr("扫描方向", "Scan direction")}
        hint={tr("常用的慢扫描方向（Nanonis scan direction）。", "Usual slow-scan direction (Nanonis scan direction).")}
      >
        <SelectField
          value={scanDir}
          onChange={commitScanDir}
          options={SCAN_DIRECTION_CHOICES}
        />
      </Field>
      <Field
        label={tr("其他偏好 (自由文本)", "Other preferences (free text)")}
        hint={tr(
          "例如：优先常温、勿超过 1 V、避免高电流…（会原样告知 agent）",
          "For example: prefer room temperature, do not exceed 1 V, avoid high currents… (passed to the agent as written)",
        )}
      >
        <textarea
          defaultValue={notes}
          key={notes}
          rows={2}
          disabled={saving}
          placeholder={tr("可选", "Optional")}
          onBlur={(e) => {
            if (e.target.value.trim() !== notes.trim()) commitNotes(e.target.value);
          }}
          className="w-full rounded border border-mast-border bg-mast-bg px-2 py-1 text-sm text-mast-text disabled:opacity-50"
        />
      </Field>
      <div className="flex items-center gap-3 pt-1">
        <button
          type="button"
          onClick={resetDefaults}
          disabled={saving || !anySet}
          className="rounded border border-mast-border bg-mast-panel px-3 py-1 text-xs text-mast-muted hover:text-mast-text disabled:opacity-50"
        >
          {tr("全部清空", "Clear all")}
        </button>
        {!anySet && <span className="text-xs text-mast-muted">{tr("（未设置任何偏好）", "(no preferences set)")}</span>}
      </div>
      {saving && <p className="text-xs text-mast-muted">{tr("保存中…", "Saving…")}</p>}
    </div>
  );
}

// ── Instrument profile (retract on sample change / approach dI/dV) ─────────────
// Per-rig hardware facts the approach/retract logic depends on, plus a read-only
// view of the auto-learned dI/dV-at-contact calibration.
const INSTRUMENT_NUM_FIELDS: {
  key: string; label: string; hint: string; min: number; max: number; step: string; placeholder: string;
}[] = [
  { key: "z_recede_min_nm", label: tr("退针 Z 自检阈值", "Retract Z self-check threshold"), placeholder: tr("如 1", "e.g. 1"), min: 0, max: 1e4, step: "0.1",
    hint: tr(
      "退针每级开反馈后，Z 压电朝伸长方向移动超过此阈值（nm）才判定「确认在远离」。",
      "After feedback is switched on at each retract stage, the Z piezo must move toward extension by more than this threshold (nm) before “moving away” is confirmed.",
    ) },
  { key: "z_settle_timeout_s", label: tr("退针 Z 稳定预算", "Retract Z settle budget"), placeholder: tr("如 20", "e.g. 20"), min: 0.5, max: 300, step: "0.5",
    hint: tr(
      "退针自检等 Z 反馈稳定下来的**上限**，不是等待时间——Z 一停就立刻读。只在超时放弃时用到：判定会如实报「没得出结论」而不是报「逼近」。默认 20 秒留了余量；若日志里出现超时且报的漂移速率仍很大，说明这台机器响应更慢，调大它。",
      "The upper bound on how long the retract self-check waits for the Z feedback to settle; it is not a fixed wait, Z is read as soon as it stops. It only matters when the wait times out: the verdict then honestly reports “no conclusion” instead of “approaching”. The default of 20 s leaves margin; if the log shows timeouts with a still-large drift rate, the machine responds more slowly, so raise it.",
    ) },
  { key: "retract_total_steps", label: tr("退针总步数", "Retract total steps"), placeholder: tr("如 3000", "e.g. 3000"), min: 1, max: 1e6, step: "1",
    hint: tr(
      "换样品/关机退针的粗动马达总步数。分级 1→10→100→剩余 退完，每级 Z 自检。",
      "Total coarse-motor steps for retracting on sample change / shutdown. Done in stages 1 → 10 → 100 → remainder, with a Z self-check at each stage.",
    ) },
  { key: "retract_step_max", label: tr("单次粗动上限", "Max steps per coarse move"), placeholder: tr("如 1000", "e.g. 1000"), min: 1, max: 1000, step: "1",
    hint: tr("单次 Motor_StartMove 的最大步数（Nanonis 硬件上限 1000）。", "Maximum number of steps in one Motor_StartMove (Nanonis hardware limit 1000).") },
  { key: "lockin_signal_index", label: tr("dI/dV 信号索引", "dI/dV signal index"), placeholder: tr("如 8", "e.g. 8"), min: 0, max: 127, step: "1",
    hint: tr(
      "lock-in R (dI/dV) 输出对应的 Nanonis 信号索引（Signals_ValGet 用）。进针 dI/dV 测距 + 退针辅助自检需要它；不填则跳过 dI/dV。",
      "Nanonis signal index of the lock-in R (dI/dV) output (used by Signals_ValGet). Needed for approach dI/dV distance sensing and the retract auxiliary self-check; if blank, dI/dV is skipped.",
    ) },
  { key: "lockin_mod_amp_v", label: tr("dI/dV 调制幅度", "dI/dV modulation amplitude"), placeholder: tr("如 0.02", "e.g. 0.02"), min: 0, max: 1, step: "0.001",
    hint: tr(
      "lock-in 加在电流上的偏压调制幅度，单位 V（默认 20 mV = 0.02 V）。",
      "Bias modulation amplitude applied by the lock-in, in V (default 20 mV = 0.02 V).",
    ) },
  { key: "lockin_mod_freq_hz", label: tr("dI/dV 调制频率", "dI/dV modulation frequency"), placeholder: tr("如 973", "e.g. 973"), min: 0, max: 1e5, step: "1",
    hint: tr(
      "调制频率，单位 Hz。建议避开 50/60 Hz 工频谐波与机械共振。",
      "Modulation frequency, in Hz. Avoid harmonics of the 50/60 Hz mains and mechanical resonances.",
    ) },
  { key: "lockin_x_signal_index", label: tr("解调 X 信号索引", "Demodulated X signal index"), placeholder: tr("如 24", "e.g. 24"), min: 0, max: 127, step: "1",
    hint: tr(
      "解调器的 X 分量走哪一路 RT 信号。这是接线事实，软件读不出来；AutoPhase 缺它会拒绝对齐相位而不是猜（猜错读到的是另一路信号，算出来的角度看上去一样合理）。",
      "Which RT signal carries the demodulator's X component. This is a wiring fact the software cannot read; without it AutoPhase refuses to align the phase rather than guess (a wrong guess reads another signal and yields an equally plausible-looking angle).",
    ) },
  { key: "lockin_y_signal_index", label: tr("解调 Y 信号索引", "Demodulated Y signal index"), placeholder: tr("如 25", "e.g. 25"), min: 0, max: 127, step: "1",
    hint: tr(
      "解调器的 Y 分量走哪一路 RT 信号。同 X，两个都填了 AutoPhase 才可用。",
      "Which RT signal carries the demodulator's Y component. As for X; AutoPhase is usable only when both are filled in.",
    ) },
  { key: "approach_didv_engage_frac", label: tr("进针 dI/dV 判据比例", "Approach dI/dV engage fraction"), placeholder: tr("如 0.7", "e.g. 0.7"), min: 0, max: 2, step: "0.05",
    hint: tr(
      "进针时 dI/dV 达到「到样品标定值」的此比例，视为接触在望。",
      "During approach, contact is considered imminent once dI/dV reaches this fraction of the at-sample calibration value.",
    ) },
  // Approach preset. If the operator had to dictate three numbers before every
  // approach and the model transcribed them into a tool call, a transcription slip
  // such as 3e-12 becoming 3 (a factor 1e12 too large) would be possible. With the
  // values entered here, ApplyZCtrlPreset('approach') writes them to the hardware
  // directly from code, without going through the model.
  { key: "approach_p_gain_m", label: tr("进针 Z 比例增益 P", "Approach Z proportional gain P"), placeholder: tr("如 3e-12", "e.g. 3e-12"), min: 1e-15, max: 1e-6, step: "1e-13",
    hint: tr(
      "Nanonis 面板 Z-Controller → Proportional (m)。面板上的「3.000p」= 3e-12。进针与扫图通常用同一个 P。",
      "Nanonis panel Z-Controller → Proportional (m). “3.000p” on the panel = 3e-12. Approach and scanning usually use the same P.",
    ) },
  { key: "approach_i_gain_m_per_s", label: tr("进针 Z 积分增益 I", "Approach Z integral gain I"), placeholder: tr("如 1.8e-7", "e.g. 1.8e-7"), min: 1e-12, max: 1e-3, step: "1e-9",
    hint: tr(
      "Nanonis 面板 Z-Controller → Integral (m/s)。面板上的「180.000n」= 1.8e-7。进针比扫图快（环更快），扫图值在下方扫描档位表里单独设。",
      "Nanonis panel Z-Controller → Integral (m/s). “180.000n” on the panel = 1.8e-7. Approach uses a faster loop than scanning; the scan value is set separately in the scan policy table below.",
    ) },
  { key: "approach_setpoint_a", label: tr("进针电流设定点", "Approach current setpoint"), placeholder: tr("如 1.5e-10", "e.g. 1.5e-10"), min: 1e-12, max: 1e-7, step: "1e-12",
    hint: tr(
      "进针用的隧道电流设定点，单位安培（150 pA = 1.5e-10）。越大进针越快。扫图设定点在扫描档位表里。",
      "Tunneling-current setpoint used for approach, in amperes (150 pA = 1.5e-10). Larger values approach faster. The scan setpoint is in the scan policy table.",
    ) },
  { key: "approach_steps_per_cycle", label: tr("Auto Approach 每轮步数", "Auto Approach steps per cycle"), placeholder: tr("如 3", "e.g. 3"), min: 1, max: 1000, step: "1",
    hint: tr(
      "Nanonis Auto Approach 面板的 Number of Pulses。仅作记录与核对，MAST 不改写面板设置。",
      "Number of Pulses on the Nanonis Auto Approach panel. Recorded for reference and cross-checking only; MAST does not change the panel setting.",
    ) },
  { key: "approach_expected_steps", label: tr("进针预计总步数", "Expected approach steps"), placeholder: tr("如 2500", "e.g. 2500"), min: 1, max: 1e6, step: "100",
    hint: tr(
      "你的经验值。用于判断某次进针是否异常（远少于此值可能是提前触发，远多于可能是没在靠近）。",
      "Your empirical value. Used to judge whether an approach is abnormal (far fewer steps may mean an early trigger, far more may mean it is not getting closer).",
    ) },
  // Scan-map avoidance radii: how far the debris of one tip-conditioning event
  // actually spreads depends on this tip, this sample and this temperature, so these
  // are operator-supplied values; the defaults are only a starting point.
  { key: "avoid_radius_tip_shape_nm", label: tr("修针尖避让半径", "Tip-conditioning avoidance radius"), placeholder: tr("如 30", "e.g. 30"), min: 0, max: 1e5, step: "1",
    hint: tr(
      "修针尖点周围多大范围内不再扫图（nm）。碎屑散布范围随针尖/样品/温度变，按你的经验调。",
      "Radius around a tip-conditioning site that is no longer scanned (nm). The debris spread varies with tip, sample and temperature; tune it from experience.",
    ) },
  { key: "avoid_radius_pulse_nm", label: tr("电脉冲避让半径", "Voltage-pulse avoidance radius"), placeholder: tr("如 150", "e.g. 150"), min: 0, max: 1e5, step: "1",
    hint: tr("电脉冲点的避让半径（nm）。", "Avoidance radius around a voltage-pulse site (nm).") },
  { key: "scan_spacing_factor", label: tr("计划扫描点间距", "Planned scan spacing"), placeholder: tr("如 1.2", "e.g. 1.2"), min: 1, max: 20, step: "0.1",
    hint: tr(
      "相邻计划帧的中心相距几个扫描框。1.2 = 紧挨着排（默认，取样最密），3 = 每帧之间留两帧空。想让一片区域被更分散地采样时调大它——两种巡览策略都受它影响。",
      "Distance between the centres of adjacent planned frames, in scan-frame widths. 1.2 = packed tightly (default, densest sampling), 3 = two empty frames between frames. Raise it to sample an area more sparsely; both survey strategies are affected.",
    ) },
  { key: "avoid_radius_crash_nm", label: tr("撞针避让半径", "Tip-crash avoidance radius"), placeholder: tr("如 150", "e.g. 150"), min: 0, max: 1e5, step: "1",
    hint: tr(
      "撞针点的避让半径（nm）。撞过的地方会永久留在地图上，不随内存熔断计时器过期。",
      "Avoidance radius around a tip-crash site (nm). Crash sites stay on the map permanently and do not expire with the in-memory breaker timer.",
    ) },
  { key: "avoid_radius_approach_nm", label: tr("进针扎痕避让半径", "Approach-mark avoidance radius"), placeholder: tr("如 200", "e.g. 200"), min: 0, max: 1e5, step: "1",
    hint: tr(
      "进针点的避让半径（nm）。仅当下方「进针是否留扎痕」不是「不会」时才生效。",
      "Avoidance radius around an approach site (nm). Only applies when “Approach leaves marks” below is not set to “No”.",
    ) },
  { key: "xy_motor_step_m", label: tr("XY 粗动单步位移", "XY coarse displacement per step"), placeholder: tr("如 1e-7（可留空）", "e.g. 1e-7 (may be left blank)"), min: 0, max: 1e-3, step: "1e-9",
    hint: tr(
      "可选标定，单位米。仅用于在粗动标记上注一句「大概走了多远」；粗动是开环、无位置反馈，没有任何选点逻辑依赖它。留空则不注。",
      "Optional calibration, in metres. Only used to annotate coarse-move markers with roughly how far they went; coarse motion is open-loop with no position feedback and no site-selection logic depends on it. Leave blank to skip the annotation.",
    ) },
  // Scan geometry and levelling (AutoTilt + scan_resolver). These four are physical
  // facts of this machine; when unset they stay at defaults that are merely a
  // starting point no STM would find absurd.
  { key: "z_range_m", label: tr("Z 压电总量程", "Z piezo total range"), placeholder: tr("如 1.5e-6", "e.g. 1.5e-6"), min: 1e-9, max: 1e-4, step: "1e-9",
    hint: tr(
      "调平触发判据的分母：判据是「这一帧的斜坡吃掉多少 Z 量程」，所以同一个倾角在 1 µm 帧和 10 nm 帧上自动给出不同的紧迫程度。",
      "Denominator of the levelling trigger: the criterion is how much of the Z range the slope of this frame consumes, so the same tilt angle gets a different urgency on a 1 µm frame and a 10 nm frame.",
    ) },
  { key: "tilt_limit_deg", label: tr("倾斜补偿上限（单轴）", "Tilt compensation limit (per axis)"), placeholder: tr("如 5", "e.g. 5"), min: 0, max: 45, step: "0.1",
    hint: tr(
      "压电倾斜补偿的绝对上限（度）。补过头会吃掉 XY 行程并让 Z 在帧角上打满；5° 对任何 STM 都已是很大的失配角。",
      "Absolute limit of the piezo tilt compensation (degrees). Over-compensating consumes XY travel and saturates Z at the frame corners; 5° is already a large misalignment for any STM.",
    ) },
  { key: "v_tip_max_m_s", label: tr("针尖横向速度上限", "Max lateral tip speed"), placeholder: tr("如 2e-6", "e.g. 2e-6"), min: 1e-12, max: 1e-3, step: "1e-9",
    hint: tr(
      "单位 m/s。像素数、每线时间、帧宽单独看都合法，乘起来才知道针尖扫得多快——这个组合约束由 scan_resolver 检查。",
      "In m/s. Pixel count, time per line and frame width are each valid on their own; only their product tells how fast the tip moves. This combined constraint is checked by scan_resolver.",
    ) },
  { key: "z_noise_floor_m", label: tr("Z 噪声底", "Z noise floor"), placeholder: tr("留空 = 每次从数据估计", "blank = estimate from the data each time"), min: 0, max: 1e-9, step: "1e-13",
    hint: tr(
      "单位米。留空则每帧从数据现估（RANSAC 内点阈、圆拟合台阶否决、PI 整定验证都要用它）；填了就当本机基准值，省掉每次估计。",
      "In metres. Blank = estimated from the data for every frame (used by the RANSAC inlier threshold, the circle-fit step veto and PI-tuning verification); if filled in, it is used as this machine's baseline and the per-frame estimate is skipped.",
    ) },
  // Lateral coarse relocation (RelocateCoarseXY). The defaults are starting points;
  // true values can only be calibrated on the real instrument: move once, scan an
  // image, and check whether the terrain has changed completely.
  { key: "xy_prewithdraw_steps", label: tr("横向移动前退针步数", "Pre-withdraw steps before lateral move"), placeholder: tr("如 100", "e.g. 100"), min: 0, max: 100000, step: "1",
    hint: tr(
      "横向粗动前用粗动马达退多少步清障。压电收到顶只有 1–2 µm 余量，而样品台侧滑时的垂直跳动、样品倾斜和针尖长度都远不止这个数——这一步压电退针替代不了。",
      "How many coarse-motor steps to withdraw before a lateral coarse move to clear obstacles. A fully retracted piezo only gives 1–2 µm of margin, while the vertical jitter of the stage sliding sideways, the sample tilt and the tip length are all far larger; piezo withdrawal cannot replace this step.",
    ) },
  { key: "xy_move_chunk_steps", label: tr("横向移动分块步数", "Lateral move chunk size (steps)"), placeholder: tr("如 50", "e.g. 50"), min: 1, max: 1000, step: "1",
    hint: tr(
      "每移动这么多步就回读一次电流（+qPlus 振幅）并重评真空。块越小看护越密，但每块都有一次 TCP 往返。",
      "After this many steps the current (and qPlus amplitude) is read back and the vacuum is re-evaluated. Smaller chunks watch more closely but cost one TCP round trip each.",
    ) },
  { key: "au_step_pm", label: tr("单原子台阶高度（本机读数）", "Single-atom step height (as read on this machine)"), placeholder: tr("如 207", "e.g. 207"), min: 50, max: 500, step: "0.5",
    hint: tr(
      "pm。多针尖判据的尺子：单针尖成像时相邻台面的高度差是它的整数倍。填的是**这台仪器读出来的**高度，不是物理值 235.5 pm（Z 标定可能与物理值有偏差）。换扫描器或重标 Z 之后要重测（拿一张干净的多台阶图，看相邻台面差）。",
      "In pm. The ruler of the multi-apex criterion: with a single tip, height differences between adjacent terraces are integer multiples of it. Enter the height as read on this instrument, not the physical value of 235.5 pm (the Z calibration may deviate from it). Re-measure after changing the scanner or recalibrating Z (take a clean multi-step image and look at the differences between adjacent terraces).",
    ) },
  { key: "xy_site_spacing_steps", label: tr("粗动站点最小间距", "Min spacing between coarse sites"), placeholder: tr("如 200", "e.g. 200"), min: 1, max: 1000000, step: "1",
    hint: tr(
      "两片工作区之间至少隔多少步。必须让新区域完全跳出压电量程（±1.5 µm），否则「换区」只是把旧区域挪进视野。",
      "Minimum number of steps between two work areas. The new area must lie completely outside the piezo range (±1.5 µm), otherwise “relocating” merely moves the old area into view.",
    ) },
  { key: "xy_axis_step_budget", label: tr("单轴行程预算", "Per-axis travel budget"), placeholder: tr("如 5000", "e.g. 5000"), min: 1, max: 10000000, step: "1",
    hint: tr(
      "单位步。粗动台行程有限，走到头只会空滑（无害）但位置认知会全部丢失。超出预算的落点会被规划器拒绝。",
      "In steps. The coarse stage has limited travel; reaching the end only slips idly (harmless) but all position knowledge is lost. The planner rejects targets beyond the budget.",
    ) },
  { key: "xy_step_uncertainty_frac", label: tr("单步位移不确定度", "Per-step displacement uncertainty"), placeholder: tr("如 0.3", "e.g. 0.3"), min: 0, max: 1, step: "0.05",
    hint: tr(
      "相对值。开环步进器的步长随驱动幅度/负载/温度漂移，所以大地图上的站点画成模糊斑而不是点；这个数决定斑有多大。低温下应当调大。",
      "Relative value. The step length of an open-loop stepper drifts with drive amplitude, load and temperature, so sites on the large map are drawn as blurred patches instead of points; this number sets the patch size. Raise it at low temperature.",
    ) },
  // Coarse-motion vacuum interlock. The discharge region (near the Paschen minimum)
  // is about 0.1–1000 Pa = 1e-3–10 mbar, and pumping down or venting passes right
  // through it, which is also when someone is most likely to want coarse motion.
  { key: "coarse_motion_max_pressure_pa", label: tr("允许粗动的压强上限", "Max pressure allowing coarse motion"), placeholder: tr("如 1e-2", "e.g. 1e-2"), min: 0, max: 1e5, step: "1e-3",
    hint: tr(
      "单位 Pa。高于此值一律拒绝粗动。默认 1e-2 Pa 比放电区下沿（0.1 Pa）低一个数量级，任何真实 UHV STM 都远低于它。",
      "In Pa. Coarse motion is refused above this value. The default of 1e-2 Pa is an order of magnitude below the lower edge of the discharge region (0.1 Pa), and any real UHV STM is far below it.",
    ) },
  { key: "vacuum_reading_max_age_s", label: tr("真空计读数有效期", "Vacuum reading validity"), placeholder: tr("如 60", "e.g. 60"), min: 1, max: 3600, step: "1",
    hint: tr(
      "单位秒。超过这个时长的读数不能给现在授权——抽气/放气时压强变化很快，几分钟前的数字说明不了此刻。",
      "In seconds. A reading older than this cannot authorise anything now: pressure changes fast during pump-down or venting, and a number from a few minutes ago says nothing about this moment.",
    ) },
  { key: "vacuum_gauge_full_scale_pa", label: tr("真空计量程上限", "Vacuum gauge full-scale"), placeholder: tr("如 1e-1（DL-7）", "e.g. 1e-1 (DL-7)"), min: 1e-9, max: 1e6, step: "1e-3",
    hint: tr(
      "单位 Pa。读数到满量程 80% 以上视为「超量程、判断不了」而非「刚好卡在阈值下」——DL-7 的帧解析不校验指数位，超量程可能解出一个看着合理的小数。换规时改这里。",
      "In Pa. A reading above 80% of full scale is treated as “over range, cannot judge” rather than “just below the threshold”: the DL-7 frame parser does not validate the exponent bits, so an over-range reading may decode to a plausible-looking small number. Change this when the gauge is replaced.",
    ) },
  { key: "vacuum_gauge_min_pa", label: tr("真空计量程下限", "Vacuum gauge lower limit"), placeholder: tr("如 5e-8（DL-7）", "e.g. 5e-8 (DL-7)"), min: 1e-12, max: 1e6, step: "1e-9",
    hint: tr(
      "单位 Pa。规触底时发出的数（下限值 / 0 / 噪声）看起来正好像「真空非常好」。在冷阴极规上无害（下限远低于允许上限，触底只意味着更安全）；在**粗糙真空规**（Pirani / 电容薄膜规，下限约 0.5–1 Pa）上则是漏洞——触底只说明「低于 1 Pa」，而那包含 0.5 Pa，正在放电区里。填对这个数，只有这只规的下限本身低于允许上限时，触底读数才会放行。",
      "In Pa. The value a gauge reports when it bottoms out (its lower limit, 0 or noise) looks exactly like “very good vacuum”. On a cold-cathode gauge this is harmless (the lower limit is far below the allowed maximum, so bottoming out only means safer). On a rough-vacuum gauge (Pirani / capacitance manometer, lower limit about 0.5–1 Pa) it is a hole: bottoming out only says “below 1 Pa”, which includes 0.5 Pa, inside the discharge region. With this number set correctly, a bottomed-out reading is accepted only if this gauge's own lower limit is below the allowed maximum.",
    ) },
  // Signal chain.
  { key: "preamp_gain_v_per_a", label: tr("前置放大器跨阻增益", "Preamp transimpedance gain"), placeholder: tr("如 1e9", "e.g. 1e9"), min: 1e3, max: 1e13, step: "1e3",
    hint: tr(
      "单位 V/A：电流 = 读数电压 ÷ 此值。Nanonis 只给得到增益「索引」，索引对应多少 V/A 不在任何地方——只能在这里登记。填错一个量级，MAST 报出去的每一个电流值就整体错一个量级，而且完全静默。",
      "In V/A: current = reading voltage ÷ this value. Nanonis only exposes a gain index; what V/A the index corresponds to is recorded nowhere else, so it can only be registered here. A wrong order of magnitude makes every current MAST reports wrong by the same factor, completely silently.",
    ) },
  { key: "preamp_full_scale_a", label: tr("前置放大器满量程电流", "Preamp full-scale current"), placeholder: tr("如 1e-8（±10 nA）", "e.g. 1e-8 (±10 nA)"), min: 1e-12, max: 1e-2, step: "1e-9",
    hint: tr(
      "单位 A：这台前放能测到的最大电流（面板/手册上写的「±10 nA」那个数）。与上面的增益是同一件事的两种说法（满量程 ≈ ±10 V ÷ 增益），两个都填就能互相验一次。它是**设定点上限**与**电流贴轨判据**的物理依据——高于前放量程的设定点物理上达不到，反馈环拿不到目标电流就会一路把 Z 推向样品直到撞针。新仪器初始化页面会据它给出这两条线的建议值。",
      "In A: the largest current this preamp can measure (the “±10 nA” figure on the panel / datasheet). It is the same fact as the gain above stated differently (full scale ≈ ±10 V ÷ gain), so filling in both lets them cross-check each other. It is the physical basis of the setpoint upper limit and the current-at-rail criterion: a setpoint above the preamp range cannot be reached, and a feedback loop that cannot get the target current keeps driving Z toward the sample until the tip crashes. The new-instrument setup page uses it to suggest values for both lines.",
    ) },
];
// Free-text fields (model names and the like) that neither the numeric nor the
// enumerated spec can hold.
//
// Do not put them into INSTRUMENT_CALIB_KEYS for convenience: buildBase() below only
// echoes calibration keys with typeof === "number", so a string would be silently
// erased on the next unrelated save (the same shape as the qPlus baseline bug).
// TEXT keys need their own echo branch.
const INSTRUMENT_TEXT_FIELDS: { key: string; label: string; hint: string; placeholder: string }[] = [
  { key: "preamp_model", label: tr("前置放大器型号", "Preamp model"), placeholder: tr("如 FEMTO DLPCA-200", "e.g. FEMTO DLPCA-200"),
    hint: tr(
      "记下来是为了让 AI 解释噪声与带宽时知道自己在跟什么打交道（不同前放的噪声底、带宽、最大输入差别很大）。",
      "Recorded so the AI knows what it is dealing with when explaining noise and bandwidth (noise floor, bandwidth and maximum input differ a lot between preamps).",
    ) },
];
const RETRACT_DIR_KEY = "retract_motor_dir";
const RETRACT_DIR_CHOICES = [
  { value: "z+", label: tr("Z+ (Nanonis 标准: 远离样品)", "Z+ (Nanonis standard: away from the sample)") },
  { value: "z-", label: tr("Z− (反向装置)", "Z− (reversed setup)") },
];
const Z_EXTEND_SIGN_KEY = "z_extend_sign";
const Z_EXTEND_SIGN_CHOICES = [
  { value: "+1", label: tr("伸长时 Z 增大 (+1)", "Z increases on extension (+1)") },
  { value: "-1", label: tr("伸长时 Z 减小 (−1)", "Z decreases on extension (−1)") },
];
// Scan-map site-selection ability (decides whether relocating is an option and which
// scan path is used).
const XY_COARSE_KEY = "xy_coarse_motion";
const XY_COARSE_CHOICES = [
  { value: "yes", label: tr("有 — 可粗动换到新区域", "Yes — can move coarsely to a new area") },
  { value: "no", label: tr("无 — 换区只能靠插拔样品", "No — relocating requires swapping the sample") },
];
const APPROACH_DMG_KEY = "approach_damages_surface";
const APPROACH_DMG_CHOICES = [
  { value: "unknown", label: tr("未知 — 按保守处理（同「会」）", "Unknown — treated conservatively (same as “Yes”)") },
  { value: "yes", label: tr("会 — 避开进针点", "Yes — avoid the approach site") },
  { value: "no", label: tr("不会（好机器）— 进针点只作历史记录", "No (good machine) — approach site kept as history only") },
];
// Whether an approach can crash the tip is a separate question from the one above
// (whether it marks the surface): one costs a patch of surface, the other costs the
// tip plus hours of conditioning. It decides whether the AI may start an approach on
// its own. Currently injected into the model only, not a hard gate: the HITL gate is
// derived from safety_level when the graph is built, and a value entered at runtime
// cannot change it (see core/instrument_profile._format_approach_supervision_line).
const APPROACH_SUPERVISION_KEY = "approach_supervision";
const APPROACH_SUPERVISION_CHOICES = [
  { value: "unknown", label: tr("未知 — 按保守处理（同「必须有人在场」）", "Unknown — treated conservatively (same as “attended”)") },
  { value: "attended", label: tr("必须有人在场 — 本机进针会撞/扎针", "Attended — approach on this machine can crash/poke the tip") },
  { value: "unattended", label: tr("可无人值守 — 本机 Auto Approach 不扎针", "Unattended OK — Auto Approach on this machine does not poke the tip") },
];
// Whether AutoApproach watches the qPlus amplitude while the coarse approach runs and
// stops + withdraws when it drops to zero. Purely additive: the current
// stop, Z progress and the time budget all stay. Off is for a machine whose residual
// amplitude also drops at tunnelling distance, where the watch would stop good approaches.
const APPROACH_QPLUS_WATCH_KEY = "approach_qplus_watch";
const APPROACH_QPLUS_WATCH_CHOICES = [
  { value: "on", label: tr("开 — 途中振幅归零就停进针并退针（默认）", "On — stop and withdraw if the amplitude drops to zero on the way (default)") },
  { value: "off", label: tr("关 — 进针只靠电流判停", "Off — the approach relies on the current stop only") },
];
const SCAN_PATH_KEY = "scan_path_strategy";
const SCAN_PATH_CHOICES = [
  { value: "auto", label: tr("自动 — 按有无 XY 粗动推导", "Auto — derived from whether XY coarse motion exists") },
  { value: "center_first", label: tr("中心优先 — 压电蠕变最小", "Centre first — least piezo creep") },
  { value: "perimeter_inward", label: tr("外圈→内圈 — 可用面积利用最大化", "Outside in — makes the most of the usable area") },
];
// What to do when no trustworthy pressure reading is available. The factory default
// is "off" (no blocking; the verdict is still computed and reported).
const VACUUM_MODE_KEY = "vacuum_interlock_mode";
const VACUUM_MODE_CHOICES = [
  { value: "gauge_or_attest", label: tr("真空计优先，读不到时可由用户签署", "Gauge first; the user may attest when it cannot be read") },
  { value: "gauge_only", label: tr("只认真空计 — 读不到就绝不粗动", "Gauge only — never move coarsely without a reading") },
  { value: "off", label: tr("关闭阻断 — 仍记录裁决，风险由人承担（默认）", "Blocking off — verdict still recorded, risk is on the operator (default)") },
];
// Whether the bias is applied to the sample or to the tip decides whether positive
// bias in a dI/dV spectrum corresponds to occupied or empty states. The default is
// "unknown" rather than "sample": although sample bias is the convention on most
// machines, defaulting to the most common convention is the source of this class of
// error, and it is invisible in the data.
// Lock-in demodulation form. This is not a display preference but the physical
// meaning of the criterion: R is an amplitude, always positive and monotonically
// increasing on approach; X is a signed projection whose phase rotates during
// approach and can cross zero, so "closer means larger" would drop midway, which is
// a phase problem and not the tip retreating. On some rigs the lock-in demodulator
// exposes only X/Y and no R, so an "R signal index" label would promise something
// the hardware cannot provide.
const LOCKIN_FORM_KEY = "lockin_readout_form";
const LOCKIN_FORM_CHOICES = [
  { value: "unknown", label: tr("未声明 — 只当绝对值读数，不声称它是幅度", "Undeclared — treated as an absolute-value reading, not claimed to be an amplitude") },
  { value: "xy", label: tr("X / Y（带符号投影 — 需先把相位调到信号全落在 X 上）", "X / Y (signed projection — phase must first be set so the signal falls entirely on X)") },
  { value: "r_phi", label: tr("R / Φ（幅度 — 恒为正，随接近单调增大）", "R / Φ (amplitude — always positive, increases monotonically on approach)") },
];
const BIAS_APPLIED_KEY = "bias_applied_to";
const BIAS_APPLIED_CHOICES = [
  { value: "unknown", label: tr("未声明 — AI 会明说自己不知道，不做能态归属", "Undeclared — the AI states that it does not know and makes no state assignment") },
  { value: "sample", label: tr("样品 — 常规约定（正偏压探测样品空态）", "Sample — conventional (positive bias probes empty sample states)") },
  { value: "tip", label: tr("针尖 — 符号与常规整体反号", "Tip — all signs reversed relative to the convention") },
];
const INSTRUMENT_CHOICE_KEYS = [
  RETRACT_DIR_KEY, Z_EXTEND_SIGN_KEY, XY_COARSE_KEY, APPROACH_DMG_KEY, SCAN_PATH_KEY,
  VACUUM_MODE_KEY, BIAS_APPLIED_KEY, APPROACH_SUPERVISION_KEY, LOCKIN_FORM_KEY,
  APPROACH_QPLUS_WATCH_KEY,
];
// Learned calibration keys, written by the runtime (set_calibration) and shown
// read-only here. They must be echoed back on every save (whole-replace store).
//
// The qPlus pair is here for that reason, not because it comes from set_calibration:
// the free-oscillation baseline is captured at runtime (after a verified retract) and
// is the denominator of the amplitude crash criterion. If a key is missing from every
// list, editing any unrelated setting wipes the baseline and the detector falls back
// to no_baseline, reporting that it cannot tell while looking healthy. Anything the
// runtime writes into instrument_profile belongs in one of these lists;
// tests/v2/io/test_instrument_profile_frontend_parity.py enforces that.
const INSTRUMENT_CALIB_KEYS = [
  "didv_at_contact_v", "didv_cal_bias_v", "didv_cal_setpoint_a",
  "didv_cal_mod_amp_v", "didv_cal_updated_at",
  "qplus_amplitude_baseline", "qplus_amplitude_signal_index",
  // Written together with the baseline: whether the fork was driven when
  // it was taken (1 / 0) and the scatter of the reads. The crash check only compares a
  // reading with a baseline taken in the same excitation state, so losing the flag
  // would make a residual baseline look like a driven one.
  "qplus_amplitude_baseline_excited", "qplus_amplitude_baseline_sigma",
  // Tilt response matrix G (measured by AutoTilt), its condition number and a
  // timestamp. AutoTilt skips without this calibration, so dropping it silently
  // disables automatic levelling.
  "tilt_cal_g11", "tilt_cal_g12", "tilt_cal_g21", "tilt_cal_g22",
  "tilt_cal_cond", "tilt_cal_updated_at",
  // Measured qPlus resonance (written by AcquirePLLFreqSweep). Distinct from the
  // nominal f0/Q of the tuning fork in the tip registration: the nominal values
  // follow the tip, the measured ones belong to the fork currently installed and
  // are cleared on a tip change.
  "qplus_f0_measured_hz", "qplus_q_measured", "qplus_fq_updated_at",
];
// What the "clear calibration" button erases. A strict subset of the list above:
// echoing a key back on save and offering to delete it are different questions, and
// conflating them would let a button labelled "clear the dI/dV calibration" also
// discard the qPlus baseline, which is captured by a different mechanism and
// re-earned only by a full retract.
const INSTRUMENT_CLEARABLE_CALIB_KEYS = [
  "didv_at_contact_v", "didv_cal_bias_v", "didv_cal_setpoint_a",
  "didv_cal_mod_amp_v", "didv_cal_updated_at",
];

/** Dropdown for a value the operator must declare: while undeclared, the current value is a disabled placeholder; only choosing a real option saves; a small note below says what the item controls. */
function DeclaredChoiceSelect({
  persisted,
  choices,
  onCommit,
  note,
  undeclaredNote,
}: {
  persisted: unknown;
  choices: readonly ChoiceOption[];
  onCommit: (v: string) => void;
  note: string;
  /** Extra line shown while undeclared: what the backend currently does with it (matching the backend's actual behaviour). */
  undeclaredNote: string;
}) {
  const view = declaredChoiceView(persisted, choices);
  return (
    <div className="space-y-1">
      <SelectField
        value={view.value}
        onChange={(v) => {
          const save = choiceToSave(v, choices);
          if (save !== null) onCommit(save);
        }}
        options={view.options}
      />
      {!view.declared && <p className="text-[11px] text-mast-warn">{undeclaredNote}</p>}
      <p className="text-[11px] text-mast-faint">{note}</p>
    </div>
  );
}

function _fmtDidv(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e-3) return `${(v * 1e3).toFixed(3)} mV`;
  if (a >= 1e-6) return `${(v * 1e6).toFixed(3)} µV`;
  return `${v.toExponential(2)} V`;
}

function InstrumentProfileSection({
  settings,
  saving,
  save,
}: {
  settings: ReturnType<typeof useSettings>;
  saving: boolean;
  save: (patch: SettingsPatch) => void;
}) {
  const persisted = (settings.data?.instrument_profile ?? {}) as Record<string, unknown>;
  const numAt = (key: string): number | undefined =>
    typeof persisted[key] === "number" ? (persisted[key] as number) : undefined;
  const strAt = (key: string): string =>
    typeof persisted[key] === "string" ? (persisted[key] as string) : "";

  // Whole-replace guard: the store replaces instrument_profile wholesale. A save must
  // carry every set config field and every learned calibration key, or a config edit
  // would wipe the auto-learned dI/dV calibration.
  const buildBase = (): Record<string, unknown> => {
    const next: Record<string, unknown> = {};
    for (const f of INSTRUMENT_NUM_FIELDS) {
      const v = numAt(f.key);
      if (typeof v === "number") next[f.key] = v;
    }
    // Every enumerated key comes from one list: a choice missing here would be
    // silently reset to its default the next time any other field is edited.
    for (const k of INSTRUMENT_CHOICE_KEYS) {
      const v = strAt(k);
      if (v) next[k] = v;
    }
    // Free-text fields need their own branch: the calibration loop below only echoes
    // numbers, so a model string routed through it would be dropped on the next
    // unrelated save (the same shape as the qPlus baseline bug).
    for (const f of INSTRUMENT_TEXT_FIELDS) {
      const v = strAt(f.key);
      if (v) next[f.key] = v;
    }
    for (const k of INSTRUMENT_CALIB_KEYS) {
      if (typeof persisted[k] === "number") next[k] = persisted[k];
    }
    return next;
  };

  const commitNum = (key: string, value: number | null) => {
    const next = buildBase();
    if (value === null) delete next[key];
    else next[key] = value;
    save({ instrument_profile: next });
  };
  const commitChoice = (key: string, value: string) => {
    const next = buildBase();
    if (value) next[key] = value;
    else delete next[key];
    save({ instrument_profile: next });
  };
  const commitText = (key: string, value: string) => {
    const next = buildBase();
    const t = value.trim();
    if (t) next[key] = t;
    else delete next[key];
    save({ instrument_profile: next });
  };
  const clearCalibration = () => {
    const next = buildBase();
    for (const k of INSTRUMENT_CLEARABLE_CALIB_KEYS) delete next[k];
    save({ instrument_profile: next });
  };
  const resetAll = () => save({ instrument_profile: {} });

  const didv = numAt("didv_at_contact_v");
  const calBias = numAt("didv_cal_bias_v");
  const anySet = Object.keys(buildBase()).length > 0;

  const numRow = (key: string) => {
    const f = INSTRUMENT_NUM_FIELDS.find((x) => x.key === key)!;
    // Signal indices use a dropdown instead of a bare number. Dispatch is by key
    // because the options are the live list read back from the instrument, not one of
    // the static choice tables in this file.
    if (SIGNAL_INDEX_KEYS.has(f.key)) {
      const v = numAt(f.key);
      return (
        <Field key={f.key} label={f.label} hint={f.hint}>
          <SignalIndexField
            value={v == null ? "" : String(v)}
            onCommit={(t) => {
              const trimmed = t.trim();
              if (trimmed === "") return commitNum(f.key, null);
              const n = Number.parseInt(trimmed, 10);
              if (Number.isNaN(n)) return;
              commitNum(f.key, Math.min(f.max, Math.max(f.min, n)));
            }}
            autoValue={SIGNAL_AUTO_VALUE[f.key]}
            placeholder={f.placeholder}
          />
        </Field>
      );
    }
    return (
      <ExperimentDefaultRow
        key={f.key}
        label={f.label}
        hint={f.hint}
        value={numAt(f.key)}
        min={f.min}
        max={f.max}
        step={f.step}
        placeholder={f.placeholder}
        disabled={saving}
        onCommit={(v) => commitNum(f.key, v)}
      />
    );
  };

  const subHeading = "text-[11px] font-semibold text-mast-muted pt-1";

  return (
    <div className="max-w-lg space-y-3">
      <p className="text-xs text-mast-muted">
        {trn(
          <>
            本机<b>装置事实</b>：换样品/关机<b>退针方向</b>、进针 <b>dI/dV</b>（lock-in）参数、以及自动学习的
            <b>到样品标定值</b>。退针会用粗动马达沿此方向分级后退并逐级回读 Z 压电自检（防方向搞反撞针）；
            进针可用 dI/dV 幅度判距离。<b>退针方向即使填错也有运行时 Z 自检兜底</b>——第一级只退 1 步就能发现——
            但请如实填写。
          </>,
          <>
            Facts about this <b>rig</b>: the <b>retract direction</b> on sample change / shutdown, the approach{" "}
            <b>dI/dV</b> (lock-in) parameters, and the auto-learned <b>at-sample calibration value</b>. Retracting moves the
            coarse motor back in stages along this direction and reads the Z piezo back at each stage as a self-check (so a
            reversed direction does not crash the tip); the approach can use the dI/dV amplitude to judge distance.{" "}
            <b>A wrong retract direction is still caught by the runtime Z self-check</b>, already at the first 1-step stage,
            but please enter it truthfully.
          </>,
        )}
      </p>
      <p className={subHeading.replace(" pt-1", "")}>{tr("退针（粗动马达）", "Retract (coarse motor)")}</p>
      {/* These two must be declared by a person: while undeclared, a disabled placeholder
          is shown instead of a default value, because a displayed default cannot be told
          from a declared value, and choosing it would not count as a change and would not
          save (lib/declaredChoice.ts). There is no undo after declaring. */}
      <Field
        label={tr("退针方向", "Retract direction")}
        hint={tr(
          "粗动马达「远离样品」的方向。多数 Nanonis 装置 Z+ = 远离；反向装置选 Z−。",
          "The direction in which the coarse motor moves away from the sample. On most Nanonis setups Z+ = away; choose Z− for a reversed setup.",
        )}
      >
        <DeclaredChoiceSelect
          persisted={persisted[RETRACT_DIR_KEY]}
          choices={RETRACT_DIR_CHOICES}
          onCommit={(v) => commitChoice(RETRACT_DIR_KEY, v)}
          note={tr(
            "这一项只管粗动马达（退针时马达往哪边走）；和下面的「压电伸长符号」是两套独立的约定，不能互相推出。",
            "This only concerns the coarse motor (which way it moves when retracting); it is independent of the “Piezo extension sign” below and neither can be derived from the other.",
          )}
          undeclaredNote={tr(
            "还没声明：用到它的粗动先按出厂的 Z+（远离样品）处理并给出警告 —— 粗动反装的机器上那正是朝样品走。请照本机实际选一项。",
            "Not declared yet: coarse moves that need it are treated as the factory Z+ (away from the sample) with a warning — on a machine with a reversed coarse motor that is toward the sample. Please choose the option that matches this machine.",
          )}
        />
      </Field>
      <Field
        label={tr("压电伸长符号", "Piezo extension sign")}
        hint={tr(
          "反馈让压电伸长（趋向样品）时 Z 读数是增大还是减小——退针自检用它判方向。",
          "Whether the Z reading increases or decreases when feedback extends the piezo (toward the sample); the retract self-check uses it to judge direction.",
        )}
      >
        <DeclaredChoiceSelect
          persisted={persisted[Z_EXTEND_SIGN_KEY]}
          choices={Z_EXTEND_SIGN_CHOICES}
          onCommit={(v) => commitChoice(Z_EXTEND_SIGN_KEY, v)}
          note={tr(
            "这一项只管压电管（伸长时 Z 读数的符号）；和上面的「退针方向」是两套独立的约定，不能互相推出。",
            "This only concerns the piezo tube (the sign of the Z reading on extension); it is independent of the “Retract direction” above and neither can be derived from the other.",
          )}
          undeclaredNote={tr(
            "还没声明：退针 / 清障的方向自检判不了「远离还是靠近」，会如实说不知道（不按出厂的 +1 去猜）。请照本机实际选一项。",
            "Not declared yet: the direction self-check for retract / obstacle clearing cannot tell “away or toward” and will honestly report that it does not know (it does not guess the factory +1). Please choose the option that matches this machine.",
          )}
        />
      </Field>
      {["z_recede_min_nm", "z_settle_timeout_s", "retract_total_steps", "retract_step_max"].map(numRow)}
      <p className={subHeading}>{tr("进针 dI/dV（lock-in）", "Approach dI/dV (lock-in)")}</p>
      {["lockin_signal_index", "lockin_mod_amp_v", "lockin_mod_freq_hz",
        "lockin_x_signal_index", "lockin_y_signal_index",
        "approach_didv_engage_frac"].map(numRow)}
      <Field
        label={tr("lock-in 解调形式", "Lock-in demodulation form")}
        hint={tr(
          "上面那个信号索引指的是 X/Y 还是 R/Φ。R 是幅度、恒为正、随针尖接近单调增大；X 是对参考相位的带符号投影 —— 只有相位调到「信号全落在 X 上」时 |X| 才≈幅度，相位在接近途中转动（结电容随距离变）时 |X| 会穿零，于是「越近越大」中途掉下去，而那是相位问题不是针尖退开了。声明之后进针播报会用正确的符号并带上相位提醒。",
          "Whether the signal index above is X/Y or R/Φ. R is an amplitude: always positive and increasing monotonically as the tip approaches. X is a signed projection onto the reference phase: |X| ≈ amplitude only when the phase is set so the signal falls entirely on X, and if the phase rotates during approach (junction capacitance changes with distance) |X| can cross zero, so “closer means larger” drops midway; that is a phase problem, not the tip retreating. Once declared, the approach report uses the correct sign and adds a phase reminder.",
        )}
      >
        <SelectField
          value={strAt(LOCKIN_FORM_KEY) || "unknown"}
          onChange={(v) => commitChoice(LOCKIN_FORM_KEY, v)}
          options={LOCKIN_FORM_CHOICES}
        />
      </Field>

      <p className={subHeading}>{tr("进针参数组（Z 反馈 + 设定点）", "Approach preset (Z feedback + setpoint)")}</p>
      <p className="text-xs text-mast-muted">
        {trn(
          <>
            进针时用的 Z 参数。填在这里之后，AI 只需要说「用 approach 这组参数」
            （<code>ApplyZCtrlPreset</code>），<b>具体数值由代码取出并写入硬件，全程不经过模型</b>，
            写完还会自动回读比对。<br />
            照抄 Nanonis 面板 <b>Z-Controller → Controller Adjustment</b> 上的两个数即可：
            面板显示的 <code>3.000p</code> 就是 <code>3e-12</code>，<code>180.000n</code> 就是{" "}
            <code>1.8e-7</code>。<br />
            <b>扫图那套参数不在这里</b>——它在下面的扫描档位表里，按图幅大小自动选档，
            <code>ScanAt</code> 每帧都会下发。同一个数只存一处，改了就到处都对。
          </>,
          <>
            Z parameters used during approach. Once entered here, the AI only has to say “use the approach preset”
            (<code>ApplyZCtrlPreset</code>); <b>the actual values are fetched by code and written to the hardware, never
            passing through the model</b>, and are read back and compared afterwards.<br />
            Copy the two numbers from the Nanonis panel <b>Z-Controller → Controller Adjustment</b>: <code>3.000p</code> on the
            panel is <code>3e-12</code>, and <code>180.000n</code> is <code>1.8e-7</code>.<br />
            <b>The scanning parameters are not here</b>: they are in the scan policy table below, selected automatically by
            frame size and sent by <code>ScanAt</code> on every frame. Each number is stored in one place only, so a change is
            correct everywhere.
          </>,
        )}
      </p>
      {["approach_p_gain_m", "approach_i_gain_m_per_s", "approach_setpoint_a",
        "approach_steps_per_cycle", "approach_expected_steps"].map(numRow)}

      <p className={subHeading}>{tr("表面预算与选点（扫描地图）", "Surface budget and site selection (scan map)")}</p>
      <p className="text-xs text-mast-muted">
        {trn(
          <>
            决定 AI 怎么选下一个扫描位置、什么时候建议换区。<b>没有 XY 粗动</b>的机器，
            当前压电范围内的表面就是「插拔样品之前你能看到的全部表面」——修针尖的污染会不可逆地
            吃掉可用面积，所以要省着用。避让半径是<b>你的经验值</b>，默认值只是起点。
          </>,
          <>
            Decides how the AI picks the next scan position and when it suggests relocating. On a machine{" "}
            <b>without XY coarse motion</b>, the surface within the current piezo range is all the surface you can see
            before swapping the sample; contamination from tip conditioning irreversibly consumes usable area, so use it
            sparingly. The avoidance radii are <b>your empirical values</b>; the defaults are only a starting point.
          </>,
        )}
      </p>
      <Field
        label={tr("是否有 XY 粗动马达", "Has XY coarse motor")}
        hint={tr(
          "有粗动=一片表面用坏了可以换区；无粗动=只能靠插拔样品移位。也决定默认走哪条扫描路径。",
          "With coarse motion, a used-up surface can be abandoned by relocating; without it the only option is swapping the sample. This also decides the default scan path.",
        )}
      >
        <SelectField
          value={strAt(XY_COARSE_KEY) || "yes"}
          onChange={(v) => commitChoice(XY_COARSE_KEY, v)}
          options={XY_COARSE_CHOICES}
        />
      </Field>
      <Field
        label={tr("进针是否留扎痕", "Approach leaves marks")}
        hint={tr(
          "有些机器进针会在样品表面扎一下，好机器不会。「未知」按保守处理（当作会扎）——多让掉一点表面，比在扎痕上扫图划算。",
          "On some machines an approach pokes the sample surface; good machines do not. “Unknown” is treated conservatively (as “yes”): giving up a bit of surface is cheaper than scanning on a poke mark.",
        )}
      >
        <SelectField
          value={strAt(APPROACH_DMG_KEY) || "unknown"}
          onChange={(v) => commitChoice(APPROACH_DMG_KEY, v)}
          options={APPROACH_DMG_CHOICES}
        />
      </Field>
      <Field
        label={tr("进针要不要有人在场", "Approach needs an attendant")}
        hint={tr(
          "上一项问的是会不会点伤表面（代价：一片表面）；这一项问的是这台机器的 Auto Approach 会不会扎针（代价：针，加之后几小时的修针）。它决定 AI 敢不敢在无人值守的自主流程里自己发起进针。「未知」按保守处理（＝必须有人在场）。注意：目前这一项只会注入给模型，不是硬性审批门。",
          "The previous item asks whether the approach marks the surface (cost: a patch of surface); this one asks whether this machine's Auto Approach can crash the tip (cost: the tip plus hours of conditioning). It decides whether the AI dares to start an approach on its own in an unattended autonomous flow. “Unknown” is treated conservatively (= attended). Note: currently this is only injected into the model and is not a hard approval gate.",
        )}
      >
        <SelectField
          value={strAt(APPROACH_SUPERVISION_KEY) || "unknown"}
          onChange={(v) => commitChoice(APPROACH_SUPERVISION_KEY, v)}
          options={APPROACH_SUPERVISION_CHOICES}
        />
      </Field>
      <Field
        label={tr("进针途中盯 qPlus 振幅", "Watch the qPlus amplitude during approach")}
        hint={tr(
          "开着时，AutoApproach 起跑前先取一个静止振幅基线，途中振幅归零（激励关时单次低于基线 25% 或连续两次低于 50%；激励开时连续两次低于 10%）就停进针模块并退针——针尖不导电、扎进去也没有电流的那种撞针，只有它看得见。只做加法：电流判停照旧。若本机残余振幅在隧穿距离上也会掉、导致好好的进针被停，就关掉。",
          "When on, AutoApproach takes an at-rest amplitude baseline before it starts, and if the amplitude drops to zero on the way (excitation off: one reading below 25% of the baseline or two in a row below 50%; excitation on: two in a row below 10%) it stops the approach module and withdraws. It is the only witness to a crash with a non-conducting tip, where there is no current at all. Purely additive: the current stop still applies. Turn it off if this machine's residual amplitude also drops at tunnelling distance and good approaches get stopped.",
        )}
      >
        <SelectField
          value={strAt(APPROACH_QPLUS_WATCH_KEY) || "on"}
          onChange={(v) => commitChoice(APPROACH_QPLUS_WATCH_KEY, v)}
          options={APPROACH_QPLUS_WATCH_CHOICES}
        />
      </Field>
      <Field
        label={tr("扫描选点策略", "Scan site-selection strategy")}
        hint={tr(
          "中心优先：压电扫描管在量程中心蠕变最小，能换区的机器就该一直在中心扫、用坏了直接换区。外圈→内圈：不能换区时从边缘开始用，把最大的干净连续区域和低蠕变的中心留到最后。",
          "Centre first: the piezo scanner creeps least at the centre of its range, so a machine that can relocate should always scan at the centre and relocate once the area is used up. Outside in: when relocating is not possible, start from the edge and keep the largest clean contiguous area and the low-creep centre for last.",
        )}
      >
        <SelectField
          value={strAt(SCAN_PATH_KEY) || "auto"}
          onChange={(v) => commitChoice(SCAN_PATH_KEY, v)}
          options={SCAN_PATH_CHOICES}
        />
      </Field>
      {["avoid_radius_tip_shape_nm", "avoid_radius_pulse_nm", "avoid_radius_crash_nm",
        "avoid_radius_approach_nm", "scan_spacing_factor", "xy_motor_step_m"].map(numRow)}

      <p className={subHeading}>{tr("横向粗动换区", "Lateral coarse relocation")}</p>
      <p className="text-xs text-mast-muted">
        {trn(
          <>
            一片表面用坏了、要粗动到新的一片时用的参数。<b>换区走 RelocateCoarseXY</b>，
            它会先用粗动马达退针清障（压电退针的 1–2 µm 余量不够）、逐级自检方向、
            确认电流归零后再分块横移。这几个默认值只是起点——
            <b>真值只能实机标</b>：移一次、扫一张图、看地貌是不是<b>完全</b>换了。
          </>,
          <>
            Parameters used when a surface is used up and you move coarsely to a new one. <b>Relocation runs through
            RelocateCoarseXY</b>: it first withdraws with the coarse motor to clear obstacles (the 1–2 µm margin of piezo
            withdrawal is not enough), self-checks the direction stage by stage, and moves laterally in chunks once the
            current has dropped to zero. These defaults are only a starting point; <b>true values can only be calibrated on
            the real instrument</b>: move once, scan an image, and check whether the terrain has <b>completely</b> changed.
          </>,
        )}
      </p>
      {["xy_prewithdraw_steps", "xy_move_chunk_steps", "xy_site_spacing_steps",
        "xy_axis_step_budget", "xy_step_uncertainty_frac"].map(numRow)}

      <p className={subHeading}>{tr("多针尖判据的尺子", "Ruler of the multi-apex criterion")}</p>
      <p className="text-xs text-mast-muted">
        {trn(
          <>
            大图（≥100 nm）上会自动查「同一条台阶是不是被画了两遍」。判决用的是自相关
            重影强度（阈值按真机标注标定），这里这个数只影响报文里
            「几个台面能级」那一项 —— 但它是<b>尺子的零点</b>，填错会让台面统计整体平移。
          </>,
          <>
            On large images (≥100 nm) the system automatically checks whether the same step edge has been drawn twice. The
            verdict uses the autocorrelation ghost strength (threshold calibrated on annotated real data); this number only
            affects the “number of terrace levels” item in the report, but it is the <b>zero point of the ruler</b>, and a
            wrong value shifts the terrace statistics as a whole.
          </>,
        )}
      </p>
      {["au_step_pm"].map(numRow)}

      <p className={subHeading}>{tr("粗动真空互锁", "Coarse-motion vacuum interlock")}</p>
      <p className="text-xs text-mast-muted">
        {trn(
          <>
            在<b>中间真空区</b>（约 0.1–1000 Pa = 1e-3–10 mbar，Paschen 极小值附近）给粗动压电加
            几百伏会<b>打火击穿叠堆</b>——抽气和放气途中正好穿过这个区间。启用阻断后，MAST 只在拿到一个
            <b>量程内的有效读数</b>时才放行粗动；拿不到（没接真空计 / 计坏 / 超量程 / <b>触底</b> /
            读数过期）一律拒绝，此时需要用户在扫描地图页签署一次「当前气压安全」。
            <b>判据与规的型号无关</b>——型号相关的只有下面那两个量程数字。
            注意：<b>粗糙真空规（Pirani / 电容薄膜规）看不到那么低</b>，只装它的话粗动会一直被拒绝，
            这不是故障。
          </>,
          <>
            Applying a few hundred volts to the coarse-motion piezo in the <b>intermediate vacuum range</b> (about
            0.1–1000 Pa = 1e-3–10 mbar, near the Paschen minimum) can <b>arc and break down the stack</b>, and pumping down
            or venting passes right through it. With blocking enabled, MAST allows coarse motion only when it has a{" "}
            <b>valid in-range reading</b>; without one (no gauge connected / gauge faulty / over range / <b>bottomed out</b> /
            stale reading) it is refused, and the user must sign a “current pressure is safe” attestation on the scan map
            page. <b>The criterion does not depend on the gauge model</b>; only the two range numbers below do. Note that a{" "}
            <b>rough-vacuum gauge (Pirani / capacitance manometer) cannot read that low</b>; if it is the only gauge fitted,
            coarse motion will always be refused, which is not a fault.
          </>,
        )}
      </p>
      <Field
        label={tr("互锁模式", "Interlock mode")}
        hint={tr(
          "拿不到可信压强时怎么办。「关闭阻断」仍会计算并显示裁决——「我们选择不检查」要留得下痕迹。",
          "What to do when no trustworthy pressure is available. “Blocking off” still computes and shows the verdict, so that the choice not to check leaves a trace.",
        )}
      >
        <SelectField
          value={strAt(VACUUM_MODE_KEY) || "off"}
          onChange={(v) => commitChoice(VACUUM_MODE_KEY, v)}
          options={VACUUM_MODE_CHOICES}
        />
      </Field>
      <p className="text-[11px] text-mast-muted">
        {tr(
          "默认是「关闭阻断」：粗动不要求连接真空计（裁决仍会计算并显示）。装了真空计、希望在气压不安全时拒绝粗动的机器，请改选“真空计优先”或“只认真空计”。",
          "The default is “Blocking off”: coarse motion does not require a connected vacuum gauge (the verdict is still computed and shown). On a machine with a vacuum gauge where coarse motion should be refused at unsafe pressure, choose “Gauge first” or “Gauge only”.",
        )}
      </p>
      {["coarse_motion_max_pressure_pa", "vacuum_reading_max_age_s",
        "vacuum_gauge_full_scale_pa", "vacuum_gauge_min_pa"].map(numRow)}
      {/* The attestation form lives here so it stays reachable when the vision page
          that used to host it is hidden. */}
      <VacuumInterlockStrip />

      <p className={subHeading}>{tr("扫描几何与调平", "Scan geometry and levelling")}</p>
      <p className="text-xs text-mast-muted">
        {trn(
          <>
            自动调平和扫描参数解算用的本机物理事实。<b>倾斜响应矩阵 G</b> 由 AutoTilt 实测标定；
            <b>没有它 AutoTilt 一律跳过</b>——绝不带着猜来的符号去动硬件。
          </>,
          <>
            Physical facts of this machine used by automatic levelling and scan-parameter resolution. The{" "}
            <b>tilt response matrix G</b> is measured by AutoTilt; <b>without it AutoTilt is always skipped</b>, because the
            hardware is never driven with a guessed sign.
          </>,
        )}
      </p>
      {["z_range_m", "tilt_limit_deg", "v_tip_max_m_s", "z_noise_floor_m"].map(numRow)}

      <p className={subHeading}>{tr("信号链（前放 / 偏压极性）", "Signal chain (preamp / bias polarity)")}</p>
      <p className="text-xs text-mast-muted">
        {trn(
          <>
            这两项决定 AI 怎么<b>解释</b>数据，而不是怎么操作仪器。
            <b>偏压加在哪一侧</b>决定正偏压对应样品的空态还是占据态——弄反了整套能态归属就是反的，
            而这种错在数据里完全看不出来。<b>前放增益</b>是电流的绝对标度，
            Nanonis 只给得到「索引」，索引对应多少 V/A 只能在这里登记。
          </>,
          <>
            These two decide how the AI <b>interprets</b> the data, not how it operates the instrument. <b>Which side the
            bias is applied to</b> decides whether positive bias corresponds to empty or occupied sample states; getting it
            backwards reverses the whole state assignment, and the error is completely invisible in the data. The{" "}
            <b>preamp gain</b> is the absolute scale of the current; Nanonis only exposes an index, and what V/A the index
            corresponds to can only be registered here.
          </>,
        )}
      </p>
      <Field
        label={tr("偏压加在哪一侧", "Bias applied to")}
        hint={tr(
          "常规约定是加在样品上：正样品偏压时电子由针尖隧穿进样品空态。加在针尖上则符号整体反号。留「未声明」时 AI 会明确告诉你它不知道，而不是按最常见的约定替你断言。",
          "The usual convention is that the bias is applied to the sample: at positive sample bias electrons tunnel from the tip into empty sample states. If it is applied to the tip, all signs are reversed. When left “undeclared” the AI tells you plainly that it does not know instead of asserting the most common convention for you.",
        )}
      >
        <SelectField
          value={strAt(BIAS_APPLIED_KEY) || "unknown"}
          onChange={(v) => commitChoice(BIAS_APPLIED_KEY, v)}
          options={BIAS_APPLIED_CHOICES}
        />
      </Field>
      {INSTRUMENT_TEXT_FIELDS.map((f) => (
        <Field key={f.key} label={f.label} hint={f.hint}>
          <TextRow
            value={strAt(f.key)}
            placeholder={f.placeholder}
            disabled={saving}
            onCommit={(v) => commitText(f.key, v)}
          />
        </Field>
      ))}
      {["preamp_gain_v_per_a", "preamp_full_scale_a"].map(numRow)}

      <div className="rounded border border-mast-border bg-mast-panel px-3 py-2 text-xs text-mast-muted">
        <div className="font-semibold text-mast-text">
          {tr("到样品 dI/dV 标定值（自动学习）", "At-sample dI/dV calibration value (auto-learned)")}
        </div>
        {typeof didv === "number" ? (
          <div className="mt-1">
            {tr("当前 ≈ ", "Current ≈ ")}
            <b className="text-mast-text">{_fmtDidv(didv)}</b>
            {typeof calBias === "number"
              ? tr(`（绑定 bias ${calBias.toPrecision(3)} V）`, ` (bound to bias ${calBias.toPrecision(3)} V)`)
              : null}
            {tr(" ——每轮成功进针自动 EWMA 更新。", " — updated automatically by EWMA after each successful approach.")}
          </div>
        ) : (
          <div className="mt-1">
            {tr(
              "尚未标定：首次成功进针后自动记录，供下轮判距离用。",
              "Not calibrated yet: recorded automatically after the first successful approach and used to judge distance in later approaches.",
            )}
          </div>
        )}
        {typeof didv === "number" && (
          <button
            type="button"
            onClick={clearCalibration}
            disabled={saving}
            className="mt-2 rounded border border-mast-border bg-mast-bg px-2 py-1 text-[11px] text-mast-muted hover:text-mast-text disabled:opacity-50"
          >
            {tr("清除学习标定", "Clear learned calibration")}
          </button>
        )}
      </div>
      <div className="flex items-center gap-3 pt-1">
        <button
          type="button"
          onClick={resetAll}
          disabled={saving || !anySet}
          className="rounded border border-mast-border bg-mast-panel px-3 py-1 text-xs text-mast-muted hover:text-mast-text disabled:opacity-50"
        >
          {tr("全部恢复默认（含标定）", "Restore all defaults (including calibration)")}
        </button>
        {!anySet && <span className="text-xs text-mast-muted">{tr("（全部为默认值）", "(all at defaults)")}</span>}
      </div>
      {saving && <p className="text-xs text-mast-muted">{tr("保存中…", "Saving…")}</p>}
    </div>
  );
}

// A numeric preference row: like ThresholdRow but the value may be unset (blank), and
// clearing the field commits null (removes the preference). Enter/blur commits.
function ExperimentDefaultRow({
  label, hint, value, min, max, step, placeholder, disabled, onCommit,
}: {
  label: string; hint: string; value: number | undefined; min: number; max: number;
  step: string; placeholder: string; disabled: boolean; onCommit: (v: number | null) => void;
}) {
  const shown = typeof value === "number" ? _fmtThreshold(value) : "";
  const [text, setText] = useState(shown);
  useEffect(() => {
    setText(shown);
  }, [shown]);

  const commit = () => {
    const t = text.trim();
    if (t === "") {
      if (typeof value === "number") onCommit(null); // cleared -> drop the preference
      return;
    }
    const n = Number.parseFloat(t);
    if (Number.isNaN(n)) {
      setText(shown); // revert junk
      return;
    }
    const clamped = Math.min(max, Math.max(min, n));
    setText(_fmtThreshold(clamped));
    if (clamped !== value) onCommit(clamped);
  };

  return (
    <Field label={label} hint={hint}>
      <input
        type="text"
        inputMode="decimal"
        value={text}
        step={step}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.currentTarget as HTMLInputElement).blur();
        }}
        className="w-28 rounded border border-mast-border bg-mast-bg px-2 py-1 font-mono text-sm tabular-nums text-mast-text disabled:opacity-50"
      />
    </Field>
  );
}

// A free-text row (model names and the like). Enter/blur commits; blank clears the
// value. The caller supplies the <Field> wrapper so it can be reused inside a mapped list.
function TextRow({
  value, placeholder, disabled, onCommit,
}: {
  value: string; placeholder: string; disabled: boolean; onCommit: (v: string) => void;
}) {
  const [text, setText] = useState(value);
  useEffect(() => {
    setText(value);
  }, [value]);

  const commit = () => {
    const t = text.trim();
    if (t !== value) onCommit(t);
    else setText(value);
  };

  return (
    <input
      type="text"
      value={text}
      disabled={disabled}
      placeholder={placeholder}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.currentTarget as HTMLInputElement).blur();
      }}
      className="w-56 rounded border border-mast-border bg-mast-bg px-2 py-1 text-sm text-mast-text disabled:opacity-50"
    />
  );
}

function VisionThresholdsSection({
  settings,
  saving,
  save,
}: {
  settings: ReturnType<typeof useSettings>;
  saving: boolean;
  save: (patch: SettingsPatch) => void;
}) {
  const persisted = (settings.data?.vision_thresholds ?? {}) as Record<string, number>;
  const eff = (key: string, def: number) =>
    typeof persisted[key] === "number" ? persisted[key] : def;

  // Whole-replace guard: the store replaces the vision_thresholds dict wholesale (it
  // does not merge), so a save must POST the effective value of every knob plus the
  // one change, otherwise the other valves would be wiped.
  const commit = (key: string, value: number) => {
    const next: Record<string, number> = { ...persisted };
    for (const k of VISION_THRESHOLD_KNOBS) next[k.key] = eff(k.key, k.def);
    next[key] = value;
    save({ vision_thresholds: next });
  };

  // Empty dict: the backend from_mapping falls back to all defaults (a true reset).
  const resetDefaults = () => save({ vision_thresholds: {} });

  return (
    <div className="max-w-lg space-y-3">
      <p className="text-xs text-mast-muted">
        {trn(
          <>
            视觉模型「针尖是否合格」的判别阈值。<b>模型权重不变，只调判别的宽严。</b>
            默认偏严会把不少可用针尖判坏、触发不必要的针尖处理；
            <b>调低门槛 / 调高容忍 = 更宽松</b>。改动即时生效（无需重载模型）。
            C（图像分割）不参与好坏判定，故无阀门。
          </>,
          <>
            Thresholds the vision model uses to judge whether a tip is acceptable. <b>The model weights do not change; only
            the strictness of the judgement does.</b> The strict defaults judge many usable tips as bad and trigger
            unnecessary tip processing; <b>a lower cut / higher tolerance = more lenient</b>. Changes take effect
            immediately (no model reload). C (image segmentation) takes no part in the good/bad judgement and so has no valve.
          </>,
        )}
      </p>
      {VISION_THRESHOLD_KNOBS.map((k) => (
        <ThresholdRow
          key={k.key}
          label={k.label}
          hint={k.hint}
          value={eff(k.key, k.def)}
          min={k.min}
          max={k.max}
          step={k.step}
          disabled={saving}
          onCommit={(v) => commit(k.key, v)}
        />
      ))}
      <div className="pt-1">
        <button
          type="button"
          onClick={resetDefaults}
          disabled={saving}
          className="rounded border border-mast-border bg-mast-panel px-3 py-1 text-xs text-mast-muted hover:text-mast-text disabled:opacity-50"
        >
          {tr("恢复默认", "Restore defaults")}
        </button>
      </div>
      {saving && <p className="text-xs text-mast-muted">{tr("保存中…", "Saving…")}</p>}
    </div>
  );
}

function ClassicalThresholdsSection({
  settings,
  saving,
  save,
}: {
  settings: ReturnType<typeof useSettings>;
  saving: boolean;
  save: (patch: SettingsPatch) => void;
}) {
  const persisted = (settings.data?.classical_thresholds ?? {}) as Record<string, number>;
  const eff = (key: string, def: number) =>
    typeof persisted[key] === "number" ? persisted[key] : def;

  // Whole-replace guard: the store replaces the classical_thresholds dict wholesale,
  // so a save must POST every knob's effective value plus the change.
  const commit = (key: string, value: number) => {
    const next: Record<string, number> = { ...persisted };
    for (const k of CLASSICAL_THRESHOLD_KNOBS) next[k.key] = eff(k.key, k.def);
    next[key] = value;
    save({ classical_thresholds: next });
  };

  const resetDefaults = () => save({ classical_thresholds: {} });

  return (
    <div className="max-w-lg space-y-3">
      <p className="text-xs text-mast-muted">
        {trn(
          <>
            <b>免模型</b>的经典针尖判别工具（双针尖 / 正反扫描不稳 / 中途换针 / 反馈振荡 / I(z) 谱）的阈值，
            可<b>按仪器/样品重标定</b>。改动即时生效（无需重载任何模型）。这些是深度模型判定的<b>可解释旁路 + 性能地板</b>。
          </>,
          <>
            Thresholds of the <b>model-free</b> classical tip-quality tools (double tip / trace-retrace instability /
            mid-scan tip change / feedback oscillation / I(z) spectra), which can be <b>recalibrated per instrument or
            sample</b>. Changes take effect immediately (no model reload). They are the <b>interpretable bypass and
            performance floor</b> of the deep-model verdict.
          </>,
        )}
      </p>
      {CLASSICAL_THRESHOLD_KNOBS.map((k) => (
        <ThresholdRow
          key={k.key}
          label={k.label}
          hint={k.hint}
          value={eff(k.key, k.def)}
          min={k.min}
          max={k.max}
          step={k.step}
          disabled={saving}
          onCommit={(v) => commit(k.key, v)}
        />
      ))}
      <div className="pt-1">
        <button
          type="button"
          onClick={resetDefaults}
          disabled={saving}
          className="rounded border border-mast-border bg-mast-panel px-3 py-1 text-xs text-mast-muted hover:text-mast-text disabled:opacity-50"
        >
          {tr("恢复默认", "Restore defaults")}
        </button>
      </div>
      {saving && <p className="text-xs text-mast-muted">{tr("保存中…", "Saving…")}</p>}
    </div>
  );
}

// ── Hardware modules (moved to Advanced) ───────────────────────────────────────
// The toggles live under Advanced -> System / Global -> Capability toggles, behind the
// admin PIN. Switching a module on hands the agent DANGEROUS skills (a laser, an RF
// amplifier, probes that can collide), which is not the same class of action as
// changing the font size and should not sit next to it.
//
// A signpost stays here rather than nothing: a control that silently vanishes is
// worse than one that tells you where it went.
function HardwareModulesMoved({
  hwModules,
}: {
  hwModules: ReturnType<typeof useHardwareModules>;
}) {
  const n = hwModules.data?.modules?.length ?? 0;
  const on = hwModules.data?.enabled_count ?? 0;
  const gated = hwModules.data?.gated_skill_count ?? 0;
  return (
    <div className="max-w-lg space-y-2 text-sm text-mast-muted">
      <p>
        {trn(
          <>
            Nanonis 的选装模块（KPFM / 多探针 / 高分辨示波器 / 高速扫描器 / 射频 / 激光…），
            授权都有、硬件不一定装，<b>默认全关</b>。
          </>,
          <>
            Optional Nanonis modules (KPFM / multi-probe / high-resolution oscilloscope / high-speed scanner / RF / laser…):
            licences are all present but the hardware is not necessarily installed, so they are <b>all off by default</b>.
          </>,
        )}
      </p>
      <p>
        {trn(
          <>
            开关已移到 <b className="text-mast-text">高级 → 系统 / 全局 → 能力开关</b>，需要管理 PIN（
            <PinGuideLink>如何设置 PIN</PinGuideLink>）。
            理由：打开一个模块就等于把 <b className="text-mast-text">DANGEROUS skill</b> 交给 agent
            （激光、射频功放、会互撞的多探针）——那和「改个字号」不是同一类操作。
          </>,
          <>
            The switches have moved to <b className="text-mast-text">Advanced → System / Global → Capability toggles</b> and
            need the admin PIN (<PinGuideLink>how to set a PIN</PinGuideLink>). Reason: enabling a module hands{" "}
            <b className="text-mast-text">DANGEROUS skills</b> to the agent (a laser, an RF power amplifier, probes that can
            collide), which is not the same kind of action as changing a font size.
          </>,
        )}
      </p>
      <p className="text-xs">
        {trn(
          <>
            当前：<b className="text-mast-text">{on}</b> / {n} 个模块启用；
            <b className="text-mast-text">{gated}</b> 个 skill 因模块关闭而未挂载。
          </>,
          <>
            Currently: <b className="text-mast-text">{on}</b> / {n} modules enabled;{" "}
            <b className="text-mast-text">{gated}</b> skills are not mounted because their module is off.
          </>,
        )}
      </p>
    </div>
  );
}

function OtherSection() {
  return (
    <div className="space-y-3 text-sm text-mast-muted">
      <p>
        {trn(
          <>
            局域网访问 (LAN)、自动更新 / 推送服务器等进程级设置在 <b>桌面启动器 (Tk Launcher)</b> 中配置，
            不在本 Web 界面内。请在启动器窗口调整。
          </>,
          <>
            Process-level settings such as LAN access and the auto-update / push server are configured in the{" "}
            <b>desktop launcher (Tk Launcher)</b>, not in this web interface. Adjust them in the launcher window.
          </>,
        )}
      </p>
      <p>
        <i>
          {trn(
            <>
              系统配置编辑器（安全限值 / 技能元数据 / 知识库 / 技能指导 / 百科配置 / 复杂技能）以及
              硬件连接（Nanonis TCP / 真空计 / 温度计）在独立的「高级管理」标签内，需启动器设置的 PIN 解锁
              （<PinGuideLink>如何设置 PIN</PinGuideLink>）。
            </>,
            <>
              The system configuration editor (safety limits / skill metadata / knowledge base / skill guidance / wiki
              configuration / composite skills) and the hardware connection (Nanonis TCP / vacuum gauge / thermometer) are in
              the separate “Advanced” tab, which is unlocked with the PIN set in the launcher (
              <PinGuideLink>how to set a PIN</PinGuideLink>).
            </>,
          )}
        </i>
      </p>
    </div>
  );
}

// ── Model capability table (read-only) ─────────────────────────────────────────
function CapabilitiesSection({ models }: { models: ReturnType<typeof useModels> }) {
  if (models.isPending) return <Spinner />;
  if (models.isError) return <ErrorNote error={models.error} />;
  if (!models.data?.models?.length) return <EmptyNote label={tr("无可用模型", "No models available")} />;

  return (
    <div className="space-y-3">
      <p className="text-xs text-mast-muted">
        {tr(
          "当前模型能力（只读）。思考列：可调 = Anthropic / MiniMax 档位真正生效；固定 = 推理模型服务端固定；无 = 不传 thinking 参数。",
          "Current model capabilities (read-only). Thinking column: tunable = Anthropic / MiniMax levels take real effect; fixed = fixed server-side by the reasoning model; none = no thinking parameter is sent.",
        )}
      </p>
      <ModelCapabilityTable models={models.data.models} />
    </div>
  );
}
