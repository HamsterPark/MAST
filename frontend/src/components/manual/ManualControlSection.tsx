// Optics page "Manual control" (contract §13.1): the basic buttons of every motorised device and
// the alignment camera on one page.
//
// Sticky header: "Stop all optics motion" + tip / feedback state + the instrument token holder.
// Below, devices are grouped by machine, one card per device, rendered entirely from the
// GET /api/manual/devices description (the frontend hard-codes no device list).
//
// This section uses **no modal overlays**: confirmations happen inside the cards, and the stop
// button is reachable at all times.
//
// Local readouts are polled here in one place (GET /api/manual/readouts?ids=..., one request per
// beat, each device at its own interval_s, not through the skill-execution entry and not
// recorded) and handed to the cards through ManualReadoutsContext.
//
// On wide screens (xl) the alignment camera sits in a sticky right-hand column so the frame can
// be watched while the device buttons are used; on narrow screens it comes last.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import { Link } from "react-router-dom";
import { EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { tr } from "@/lib/i18n";
import { useHardwareModules } from "@/api/hardwareModules";
import { useInstrumentHolder, useManualDevices, useManualReadouts, useTipState } from "@/api/manual";
import { runSkill } from "@/components/skills/SkillRunButton";
import { ManualReadoutsContext, type ManualReadoutsValue } from "@/hooks/useManual";
import { DEGRADED_SOURCE_LABEL, groupDevices, readoutPlan, type DeviceGroup } from "@/lib/manual/protocol";
import { STOP_ALL_STEPS, stopAllOptics, type StopStepResult } from "@/lib/manual/stopAll";
import { moduleEnabled, TERS_OPTICS_MODULE } from "@/lib/satellites";
import { CameraPanel } from "./CameraPanel";
import { ManualDeviceCard } from "./ManualDeviceCard";

export const MANUAL_SECTION_ID = "manual";

/** Tailwind's xl breakpoint: the camera column is sticky only at this width. */
const WIDE_QUERY = "(min-width: 1280px)";

function useWide(): boolean {
  const [wide, setWide] = useState(() => typeof window !== "undefined" && window.matchMedia?.(WIDE_QUERY).matches === true);
  useEffect(() => {
    const mq = window.matchMedia?.(WIDE_QUERY);
    if (!mq) return;
    const on = () => setWide(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return wide;
}

function StopAllButton() {
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<StopStepResult[] | null>(null);
  const go = async () => {
    setBusy(true);
    setRes(null);
    const out = await stopAllOptics(async (skill, params) => {
      const o = await runSkill(skill, params);
      return o.kind === "ok" ? { ok: true, message: o.resp.summary || tr("已停", "Stopped") } : { ok: false, message: o.message };
    });
    setRes(out);
    setBusy(false);
  };
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <button
        type="button"
        onClick={() => void go()}
        className="rounded-mast-ctl border border-mast-danger-border bg-mast-danger-bg px-3 py-1.5 text-sm font-semibold text-mast-danger hover:opacity-90"
        title={STOP_ALL_STEPS.map((s) => `${s.skill} (${s.label})`).join(" → ")}
      >
        {busy ? tr("停止中…", "Stopping…") : tr("■ 停止全部光学运动", "■ Stop all optics motion")}
      </button>
      {res && (
        <ul className="space-y-0.5 text-[11px]">
          {res.map((r) => (
            <li key={r.skill} className={r.ok ? "text-mast-auto" : "text-mast-danger"}>
              {r.ok ? "✓" : "✗"} {r.label} ({r.skill}): {r.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function TipAndToken() {
  const tip = useTipState();
  const holder = useInstrumentHolder();
  const r = tip.data?.readings;
  const tipText =
    tip.isError || !r
      ? tr("读不到", "unreadable")
      : r.withdrawn === true
        ? tr("已退针", "withdrawn")
        : r.withdrawn === false
          ? tr("未退针", "not withdrawn")
          : tr("未知", "unknown");
  const fb = r?.z_controller_on === true ? tr("Z 反馈开", "Z feedback on") : r?.z_controller_on === false ? tr("Z 反馈关", "Z feedback off") : tr("Z 反馈 —", "Z feedback —");
  const scan = r?.scan_running ? tr(" · 扫描进行中", " · scan running") : "";
  const h = holder.data;
  return (
    <div className="flex flex-col gap-0.5 text-xs">
      <span className={clsx(r?.withdrawn === false ? "text-mast-warn" : "text-mast-muted")}>
        {tr("针尖：", "Tip: ")}<b className="text-mast-text">{tipText}</b> · {fb}
        {scan}
        {r?.stale ? tr("（读数陈旧）", " (stale reading)") : ""}
      </span>
      <span className={clsx(h ? "text-mast-warn" : "text-mast-muted")}>
        {tr("仪器令牌：", "Instrument token: ")}
        {holder.isError ? (
          tr("读不到", "unreadable")
        ) : h ? (
          <b>
            {tr(`${h.owner} 正在执行 ${h.skill || "某个动作"}`, `${h.owner} is running ${h.skill || "an action"}`)}
            {h.heldS !== null ? tr(`（已 ${Math.round(h.heldS)} s）`, ` (for ${Math.round(h.heldS)} s)`) : ""}
            {tr(" —— 除停止以外的按钮会被拒", " — every button except stop will be refused")}
          </b>
        ) : (
          tr("空闲", "idle")
        )}
      </span>
    </div>
  );
}

function GroupSection({ g, cols }: { g: DeviceGroup; cols: string }) {
  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-baseline gap-2">
        <h3 className="text-sm font-semibold text-mast-text">{g.title}</h3>
        <span className="text-xs text-mast-faint">{g.subtitle}</span>
      </div>
      {g.key === "nanonis" && (
        /* Unverified safety notice (contract §9 "to be verified" item: after StartMove(group=g) the
           group selected in the panel may stay on g, so a following approach from the panel or from
           AutoApproach might drive group g). Update or remove once verified on hardware. */
        <p role="note" className="rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-2.5 py-1.5 text-xs text-mast-warn">
          {tr("⚠ 动完光学组、进针之前，先看一眼 Nanonis Motor Control 面板上选中的是哪一组", "⚠ After moving an optics group and before approaching, check which group is selected in the Nanonis Motor Control panel")}
        </p>
      )}
      <div className={clsx("grid gap-3", cols)}>
        {g.devices.map((d) => (d.kind === "camera" ? <CameraPanel key={d.id} d={d} /> : <ManualDeviceCard key={d.id} d={d} />))}
      </div>
    </section>
  );
}

export function ManualControlSection() {
  const q = useManualDevices();
  const mods = useHardwareModules();
  const snap = q.data?.deployed ? q.data : null;
  const groups = snap ? groupDevices(snap.devices) : [];
  const camGroup = groups.find((g) => g.key === "camera") ?? null;
  const mainGroups = groups.filter((g) => g.key !== "camera");

  const wide = useWide();
  // Height of the sticky header (the stop-all results make it taller): the camera column sticks below it
  const headRef = useRef<HTMLDivElement | null>(null);
  const [headH, setHeadH] = useState(64);
  useEffect(() => {
    const el = headRef.current;
    if (!el) return;
    setHeadH(el.offsetHeight);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setHeadH(el.offsetHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Which local cards are on screen now (each card reports it via IntersectionObserver)
  const [visible, setVisibleSet] = useState<ReadonlySet<string>>(() => new Set());
  const setVisible = useCallback((id: string, v: boolean) => {
    setVisibleSet((prev) => {
      if (prev.has(id) === v) return prev;
      const n = new Set(prev);
      if (v) n.add(id);
      else n.delete(id);
      return n;
    });
  }, []);
  const plan = useMemo(() => (snap ? readoutPlan(snap.devices, visible) : null), [snap, visible]);
  const ro = useManualReadouts(plan);
  const ctx = useMemo<ManualReadoutsValue>(
    () => ({ readouts: ro.readouts, deployed: ro.deployed, error: ro.error, setVisible }),
    [ro.readouts, ro.deployed, ro.error, setVisible],
  );

  return (
    <ManualReadoutsContext.Provider value={ctx}>
    <div className="space-y-4">
      {/* Header: sticky so stop is reachable wherever the page is scrolled (z-20: below the top-bar emergency stop at z-30; this section has no overlay) */}
      <div
        ref={headRef}
        className="sticky top-0 z-20 -mx-1 flex flex-wrap items-start justify-between gap-3 rounded-mast-card border border-mast-border bg-mast-panel/95 px-3 py-2 shadow-mast backdrop-blur"
      >
        <StopAllButton />
        <TipAndToken />
      </div>
      <p className="text-xs text-mast-muted">
        {tr("按钮只调技能（POST /api/skills/…/execute）：安全闸门、仪器令牌、联锁、中止都照旧；停止类免令牌、不弹框。", "Buttons only call skills (POST /api/skills/…/execute): safety gate, instrument token, interlocks and abort apply as usual; stop actions need no token and show no dialog.")}
        {tr("「停止全部」依次调 ", "Stop all calls ")}{STOP_ALL_STEPS.map((s) => s.skill).join(tr("、", ", "))}{tr("（StopMotor 停 Nanonis 的所有电机组，含 STM 粗动）。", " in turn (StopMotor stops every Nanonis motor group, including the STM coarse motor).")}
      </p>
      {moduleEnabled(mods.data, TERS_OPTICS_MODULE) === false && (
        <p className="rounded-mast-ctl border border-mast-border bg-mast-bg/40 px-3 py-2 text-xs text-mast-muted">
          {tr("硬件模块 ters_optics 未开启（", "Hardware module ters_optics is off (")}
          <Link to="/settings/admin" className="text-mast-accent hover:underline">
            {tr("设置 → 高级管理 → 系统 / 全局 → 能力开关", "Settings → Advanced → System / Global → Capability switches")}
          </Link>
          {tr("，需要管理 PIN）：界面上的按钮照常可用，只是 agent 看不到这些技能。", ", admin PIN required): the buttons here still work; only the agent cannot see these skills.")}
        </p>
      )}

      {q.isPending && <Spinner label={tr("读取装置清单…", "Loading device list…")} />}
      {q.isError && <ErrorNote error={q.error} label={tr("装置清单读取失败", "Failed to load the device list")} />}
      {q.data && !q.data.deployed && (
        <EmptyNote label={tr("后端还没有手动控制的描述端点（GET /api/manual/devices 返回 404）—— 升级 MAST 后这里按装置列出按钮。", "The backend has no manual-control description endpoint yet (GET /api/manual/devices returned 404). After upgrading MAST, buttons will be listed here per device.")} />
      )}
      {snap?.degraded && (
        <div className="rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-xs text-mast-warn">
          {tr("有一部分装置读不到", "Some devices could not be read")}
          {snap.degradedParts.length ? tr(`（${snap.degradedParts.map((p) => DEGRADED_SOURCE_LABEL[p] ?? p).join("、")}）`, ` (${snap.degradedParts.map((p) => DEGRADED_SOURCE_LABEL[p] ?? p).join(", ")})`) : ""}
          {tr("：下面可能不全。", ": the list below may be incomplete.")}
          {snap.notes.length > 0 && <div className="mt-0.5">{snap.notes.join(tr("；", "; "))}</div>}
        </div>
      )}
      {snap && !snap.degraded && snap.notes.length > 0 && <p className="text-xs text-mast-faint">{snap.notes.join(tr("；", "; "))}</p>}
      {snap && snap.devices.length === 0 && <EmptyNote label={tr("后端没有报告任何电动装置。", "The backend reported no motorised devices.")} />}

      <div className={clsx(camGroup && "xl:grid xl:grid-cols-[minmax(0,1fr)_minmax(380px,40%)] xl:items-start xl:gap-4")}>
        <div className="min-w-0 space-y-4">
          {mainGroups.map((g) => (
            <GroupSection key={g.key} g={g} cols={camGroup ? "min-[1900px]:grid-cols-2" : "xl:grid-cols-2"} />
          ))}
        </div>
        {camGroup && (
          <aside
            className="mt-4 min-w-0 xl:sticky xl:mt-0 xl:overflow-y-auto"
            // 9rem: the navigation bar above the page plus bottom margin (the scroll container does not start at the viewport top)
            style={wide ? { top: headH + 12, maxHeight: `calc(100vh - ${headH + 12}px - 9rem)` } : undefined}
          >
            <GroupSection g={camGroup} cols="grid-cols-1" />
          </aside>
        )}
      </div>
    </div>
    </ManualReadoutsContext.Provider>
  );
}
