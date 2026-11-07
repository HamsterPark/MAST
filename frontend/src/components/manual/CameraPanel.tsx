// Alignment camera panel (contract §13.1): connect / disconnect, start / stop live view, snapshot,
// exposure and gain (debounced, then a skill call), the frame (frame.jpg polled at the chosen
// frame rate, never sending the next request before the previous one returned; optional
// brightness stretch for dim spots), a crosshair (frame centre by default, click to move it), and
// the centroid marker and FWHM readout (from /stats). Nothing is polled while the panel is off screen.
//
// MAST does not open the camera by default; it is opened by "Connect". Only one program can use
// the camera at a time; the backend's message (e.g. "in use by ThorImageCAM") is shown as is and
// MAST never closes other programs. While the camera is in an error state every action except
// "Disconnect" is rejected, so Disconnect is always enabled. The buttons are the skills named in
// the device description (camera_connect, live_start, ...); camera skills do not take the
// instrument token.

import { useCallback, useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { useQuery } from "@tanstack/react-query";
import { Badge, Card } from "@/components/ui";
import { tr } from "@/lib/i18n";
import { NumberField, SelectField } from "@/components/controls";
import { api } from "@/api/client";
import { runSkill } from "@/components/skills/SkillRunButton";
import { useRefreshManualDevicesSoon } from "@/api/manual";
import { useUnlock } from "@/hooks/useManual";
import { CAMERA_STATE_LABEL, cameraIdOf, clampFps, normalizeCameraStats, toRelative, type Pt } from "@/lib/manual/camera";
import { actionHints } from "@/lib/manual/plan";
import { HintList } from "./HintList";
import { actionGate, buildActionParams, busyHolderText, inputParams, type ManualAction, type ManualDevice } from "@/lib/manual/protocol";

type FrameState =
  | { status: "idle" }
  | { status: "live"; url: string }
  | { status: "not_live" }
  | { status: "error"; message: string };

type PreviewMode = "gray" | "color";

/** Polls frame.jpg at the frame rate: the next request is sent only after the previous one returned; stops while the page is hidden; 404 = not live (backs off to once per 2 s). */
function useCameraFrames(
  cameraId: string,
  fps: number,
  stretch: boolean,
  mode: PreviewMode,
  enabled: boolean,
): FrameState {
  const [st, setSt] = useState<FrameState>({ status: "idle" });
  useEffect(() => {
    if (!enabled) {
      setSt({ status: "idle" });
      return;
    }
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let prevUrl: string | null = null;
    const tick = async () => {
      if (!alive) return;
      if (document.visibilityState !== "visible") {
        timer = setTimeout(tick, 500);
        return;
      }
      const t0 = performance.now();
      let next = 1000 / fps;
      try {
        const query =
          mode === "color"
            ? { mode: "color" as const, q: 85, ...(stretch ? { scale: "auto" as const } : {}) }
            : { mode: "gray" as const, max_w: 960, q: 80, ...(stretch ? { scale: "auto" as const } : {}) };
        const { data, response } = await api.GET("/api/camera/{camera_id}/frame.jpg", {
          params: { path: { camera_id: cameraId }, query },
          parseAs: "blob",
          cache: "no-store",
        });
        if (!alive) return;
        if (response.status === 404) {
          setSt({ status: "not_live" });
          next = 2000;
        } else if (!response.ok || !(data instanceof Blob)) {
          setSt({ status: "error", message: `HTTP ${response.status}` });
          next = 2000;
        } else {
          const url = URL.createObjectURL(data);
          if (!alive) {
            URL.revokeObjectURL(url);
            return;
          }
          setSt({ status: "live", url });
          if (prevUrl) URL.revokeObjectURL(prevUrl);
          prevUrl = url;
        }
      } catch (e) {
        if (!alive) return;
        setSt({ status: "error", message: e instanceof Error ? e.message : String(e) });
        next = 2000;
      }
      timer = setTimeout(tick, Math.max(0, next - (performance.now() - t0)));
    };
    void tick();
    return () => {
      alive = false;
      if (timer !== undefined) clearTimeout(timer);
      if (prevUrl) URL.revokeObjectURL(prevUrl);
    };
  }, [cameraId, fps, stretch, mode, enabled]);
  return st;
}

/** Whether the panel is on screen (100 px margin above and below); environments without IntersectionObserver count as on screen. */
function useInView(): [(el: HTMLElement | null) => void, boolean] {
  const [inView, setInView] = useState(true);
  const obs = useRef<IntersectionObserver | null>(null);
  const ref = useCallback((el: HTMLElement | null) => {
    obs.current?.disconnect();
    obs.current = null;
    if (!el || typeof IntersectionObserver === "undefined") {
      setInView(true);
      return;
    }
    const o = new IntersectionObserver((es) => setInView(es.some((e) => e.isIntersecting)), { rootMargin: "100px 0px" });
    o.observe(el);
    obs.current = o;
  }, []);
  useEffect(() => () => obs.current?.disconnect(), []);
  return [ref, inView];
}

function fmt(v: number | null, digits = 1, unit = ""): string {
  return v === null ? "—" : `${v.toFixed(digits)}${unit}`;
}

const ROI_KEYS = new Set(["x", "y", "w", "h", "binning"]);

export function CameraPanel({ d }: { d: ManualDevice }) {
  const cameraId = cameraIdOf(d);
  const u = useUnlock(d.id);
  const refreshSoon = useRefreshManualDevicesSoon();
  const act = (id: string) => d.actions.find((a) => a.id === id) ?? null;
  const [viewRef, inView] = useInView();
  const [view, setView] = useState(true);
  const [stretch, setStretch] = useState(false);
  const [previewMode, setPreviewMode] = useState<PreviewMode>("gray");
  const [fps, setFps] = useState(4);
  const [cross, setCross] = useState<Pt>({ x: 0.5, y: 0.5 });
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [pending, setPending] = useState<{ a: ManualAction; params: Record<string, unknown> } | null>(null);
  const [exposure, setExposure] = useState("");
  const [gain, setGain] = useState("");
  const [otherVals, setOtherVals] = useState<Record<string, string>>({});
  const dirty = useRef<{ exposure: boolean; gain: boolean }>({ exposure: false, gain: false });

  const polling = view && inView;
  const statsEveryMs = Math.max(500, d.readout?.kind === "poll" ? d.readout.intervalS * 1000 : 1000);
  const stats = useQuery({
    queryKey: ["camera", cameraId, "stats"],
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/camera/{camera_id}/stats", {
        params: { path: { camera_id: cameraId } },
        cache: "no-store",
      });
      // 404 only means this camera does not exist (when not live it still returns 200 with null stats)
      if (response.status === 404) return null;
      if (error || !data) throw new Error(`HTTP ${response.status}`);
      return normalizeCameraStats(data);
    },
    enabled: polling,
    refetchInterval: statsEveryMs,
    refetchIntervalInBackground: false,
  });
  const s = stats.data ?? null;
  // Fetch frames only while /stats reports live (frame.jpg is always 404 otherwise, so avoid hitting it every 2 s)
  const frame = useCameraFrames(cameraId, fps, stretch, previewMode, polling && s?.live === true);

  // Seed the inputs from the camera's current settings (only if untouched, so a value being typed is not overwritten)
  useEffect(() => {
    if (s?.exposureUs != null && !dirty.current.exposure && exposure === "") setExposure(String(s.exposureUs));
    if (s?.gainDb != null && !dirty.current.gain && gain === "") setGain(String(s.gainDb));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s?.exposureUs, s?.gainDb]);

  const run = async (a: ManualAction, params: Record<string, unknown>) => {
    u.touch();
    setResult({ ok: true, text: tr(`${a.label}：执行中…`, `${a.label}: running…`) });
    const o = await runSkill(a.skill, params);
    if (o.kind === "ok") setResult({ ok: true, text: tr(`${a.label}：完成${o.resp.summary ? `：${o.resp.summary}` : ""}`, `${a.label}: done${o.resp.summary ? `: ${o.resp.summary}` : ""}`) });
    else {
      const busy = busyHolderText(o.resp?.data);
      setResult({ ok: false, text: tr(`${a.label}：${busy ? `被拒：${busy}。` : ""}${o.message}`, `${a.label}: ${busy ? `refused: ${busy}. ` : ""}${o.message}`) });
    }
    // State (connected / live / holder) changed: re-read stats now, refresh the device list after a short pause
    void stats.refetch();
    refreshSoon();
  };

  const trigger = (a: ManualAction | null, values: Record<string, string> = {}) => {
    if (!a) return;
    const built = buildActionParams(a, { values });
    if (!built.ok) {
      setResult({ ok: false, text: tr(`${a.label}：${built.error}`, `${a.label}: ${built.error}`) });
      return;
    }
    if (actionGate(a, u.unlocked) === "confirm") setPending({ a, params: built.params });
    else void run(a, built.params);
  };

  // Exposure / gain: call the skill 700 ms after the last edit (no request per keystroke)
  const expAct = act("set_exposure");
  const gainAct = act("set_gain");
  useEffect(() => {
    if (!dirty.current.exposure || !expAct || !exposure.trim()) return;
    const t = setTimeout(() => {
      dirty.current.exposure = false;
      const p = inputParams(expAct)[0];
      if (p) trigger(expAct, { [p.name]: exposure });
    }, 700);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [exposure]);
  useEffect(() => {
    if (!dirty.current.gain || !gainAct || !gain.trim()) return;
    const t = setTimeout(() => {
      dirty.current.gain = false;
      const p = inputParams(gainAct)[0];
      if (p) trigger(gainAct, { [p.name]: gain });
    }, 700);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gain]);

  const btn = (id: string) => {
    const a = act(id);
    if (!a) return null;
    return (
      <button
        key={id}
        type="button"
        // In an error state only Disconnect works, so it is always enabled
        disabled={!d.available && id !== "camera_disconnect"}
        onClick={() => trigger(a)}
        title={a.hint ?? undefined}
        className="rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-2.5 py-1.5 text-sm text-mast-text hover:bg-mast-panel-2 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {a.label}
      </button>
    );
  };

  const rel = s?.centroidPx && natural ? toRelative(s.centroidPx, s, natural.w, natural.h) : null;
  const known = new Set(["camera_connect", "camera_disconnect", "live_start", "live_stop", "snapshot", "set_exposure", "set_gain"]);
  const others = d.actions.filter((a) => !known.has(a.id));
  const hints = actionHints(d.actions);
  const state = s?.state ?? null;
  const notLiveText =
    state === "error"
      ? tr("相机出错：先「断开」再「连接」", "Camera error: disconnect, then connect again")
      : state === "connected"
        ? tr("已连接，没在直播：按「开始直播」", "Connected, not live: press Start live view")
        : state === "disconnected"
          ? tr("没连上：先「连接」再「开始直播」", "Not connected: connect first, then start live view")
          : tr("没在直播：先「连接」再「开始直播」", "Not live: connect first, then start live view");
  const otherDefault = (a: ManualAction, name: string, def: unknown): string => {
    // The set-ROI fields start from the camera's current ROI
    if (a.id === "set_roi" && s?.roi && ROI_KEYS.has(name)) return String(s.roi[name as keyof typeof s.roi]);
    return def != null ? String(def) : "";
  };

  return (
    <div ref={viewRef} className="min-w-0">
      <Card className="min-w-0">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-sm font-semibold text-mast-text">{d.label}</span>
              <Badge>{tr("相机", "Camera")}</Badge>
              {d.available ? <Badge tone="AUTO">{tr("可用", "Available")}</Badge> : <Badge tone="WARN">{tr("不可用", "Unavailable")}</Badge>}
              {state && (
                <Badge tone={state === "live" ? "AUTO" : state === "error" ? "WARN" : "default"}>{CAMERA_STATE_LABEL[state] ?? state}</Badge>
              )}
              {/* Only means that program is running and MAST is not connected to this camera; if the camera really is held, Connect reports it */}
              {d.occupiedBy && <Badge tone="WARN">{tr(`${d.occupiedBy} 在运行（可能占着相机）`, `${d.occupiedBy} is running (may be holding the camera)`)}</Badge>}
            </div>
            <div className="mt-0.5 font-mono text-[11px] text-mast-faint">
              {[d.vendor, d.model, d.serial && `SN ${d.serial}`, d.id].filter(Boolean).join(" · ")}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {btn("camera_connect")}
            {btn("camera_disconnect")}
            {btn("live_start")}
            {btn("live_stop")}
            {btn("snapshot")}
          </div>
        </div>

        {!d.available && (
          <div className="mt-2 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-1.5 text-xs text-mast-warn">
            {tr("不可用：", "Unavailable: ")}{d.unavailableReason || tr("后端没给原因", "no reason given by the backend")}
          </div>
        )}
        {state === "error" && (
          <div className="mt-2 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-1.5 text-xs text-mast-warn">
            {tr(`相机出错${s?.error ? `：${s.error}` : ""}。除「断开」外的操作都会被拒：先「断开」再「连接」。`, `Camera error${s?.error ? `: ${s.error}` : ""}. Every action except Disconnect is refused: disconnect, then connect again.`)}
          </div>
        )}
        {pending && (
          <div className="mt-2 flex flex-wrap items-center gap-2 rounded-mast-ctl border border-mast-border-strong bg-mast-panel-2 px-3 py-2 text-xs">
            <span>
              {tr(`执行 ${pending.a.label}（${pending.a.skill}）？`, `Run ${pending.a.label} (${pending.a.skill})?`)}
            </span>
            <button
              type="button"
              className="rounded-mast-ctl bg-mast-accent px-2.5 py-1 font-semibold text-mast-accent-ink"
              onClick={() => {
                const p = pending;
                setPending(null);
                void run(p.a, p.params);
              }}
            >
              {tr("确认执行", "Confirm")}
            </button>
            <button type="button" className="text-mast-muted hover:text-mast-text" onClick={() => setPending(null)}>
              {tr("取消", "Cancel")}
            </button>
          </div>
        )}
        {result && (
          <div role={result.ok ? "status" : "alert"} className={clsx("mt-2 text-xs", result.ok ? "text-mast-auto" : "text-mast-danger")}>
            {result.text}
          </div>
        )}

        <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-mast-muted">
          <label className="inline-flex items-center gap-1.5">
            <input type="checkbox" checked={view} onChange={(e) => setView(e.target.checked)} />
            {tr("显示画面", "Show frame")}
          </label>
          <label className="inline-flex items-center gap-1.5" title={tr("按分位拉伸亮度：暗光斑也看得见；判断饱和要关掉它看如实亮度", "Stretch brightness by percentile so dim spots are visible; turn it off to judge saturation from the true brightness")}>
            <input type="checkbox" checked={stretch} onChange={(e) => setStretch(e.target.checked)} />
            {tr("拉伸亮度", "Stretch brightness")}
          </label>
          <span className="inline-flex items-center gap-1.5" title={tr("灰度：对焦用（可缩宽）；彩色：全分辨率 Bayer 解马赛克，不下采样", "Gray: for focusing (width may be reduced). Color: full-resolution Bayer demosaic, no downsampling")}>
            {tr("画面", "View")}
            <span className="inline-block [&_select]:py-1">
              <SelectField
                value={previewMode}
                onChange={(v) => setPreviewMode(v === "color" ? "color" : "gray")}
                options={[
                  { value: "gray", label: tr("灰度", "Gray") },
                  { value: "color", label: tr("彩色·全分辨率", "Color · full resolution") },
                ]}
              />
            </span>
          </span>
          <span className="inline-flex items-center gap-1.5">
            {tr("帧率", "Frame rate")}
            <span className="inline-block [&_select]:py-1">
              <SelectField
                value={String(fps)}
                onChange={(v) => setFps(clampFps(Number(v)))}
                options={[1, 2, 4, 6, 8, 10].map((n) => ({ value: String(n), label: `${n} fps` }))}
              />
            </span>
          </span>
          <button type="button" className="text-mast-accent hover:underline" onClick={() => setCross({ x: 0.5, y: 0.5 })}>
            {tr("十字线回到中心", "Reset crosshair")}
          </button>
          <span className="text-mast-faint">{tr("点画面挪十字线", "Click the frame to move the crosshair")}</span>
        </div>

        <div className="mt-2">
          {polling && s?.live === true && frame.status === "live" ? (
            <div className="relative inline-block max-w-full select-none">
              <img
                src={frame.url}
                alt={tr("相机画面", "Camera frame")}
                className={clsx(
                  "block max-w-full cursor-crosshair rounded-mast-ctl border border-mast-border",
                  previewMode === "color" ? "max-h-[720px]" : "max-h-[480px]",
                )}
                onLoad={(e) => setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
                onClick={(e) => {
                  const r = e.currentTarget.getBoundingClientRect();
                  setCross({ x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height });
                }}
                draggable={false}
              />
              <svg className="pointer-events-none absolute inset-0 h-full w-full" viewBox="0 0 100 100" preserveAspectRatio="none">
                <line x1={cross.x * 100} y1="0" x2={cross.x * 100} y2="100" stroke="#22d3ee" strokeWidth="1" strokeOpacity="0.8" vectorEffect="non-scaling-stroke" />
                <line x1="0" y1={cross.y * 100} x2="100" y2={cross.y * 100} stroke="#22d3ee" strokeWidth="1" strokeOpacity="0.8" vectorEffect="non-scaling-stroke" />
                {rel && natural && (
                  // The viewBox is 100×100 stretched to the frame: scale the radii by the aspect ratio so the marker is a circle
                  <ellipse
                    cx={rel.x * 100}
                    cy={rel.y * 100}
                    rx="1.6"
                    ry={(1.6 * natural.w) / natural.h}
                    fill="none"
                    stroke="#f97316"
                    strokeWidth="1.5"
                    vectorEffect="non-scaling-stroke"
                  />
                )}
              </svg>
            </div>
          ) : (
            <div className="flex h-40 items-center justify-center rounded-mast-ctl border border-dashed border-mast-border-strong text-xs text-mast-muted">
              {!view
                ? tr("画面已关（不轮询）", "Frame hidden (not polling)")
                : !inView
                  ? tr("面板不在屏幕上（不轮询）", "Panel is off screen (not polling)")
                  : s && s.live !== true
                    ? notLiveText
                    : frame.status === "not_live"
                      ? notLiveText
                      : frame.status === "error"
                        ? tr(`取画面失败：${frame.message}`, `Failed to fetch frame: ${frame.message}`)
                        : tr("取画面中…", "Fetching frame…")}
            </div>
          )}
        </div>

        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-mast-muted">
          {stats.data === null && <span className="text-mast-warn">{tr("后端没有这台相机（/stats 404）", "The backend has no such camera (/stats 404)")}</span>}
          {stats.isError && <span className="text-mast-warn">{tr("读不到相机状态：", "Cannot read camera state: ")}{String(stats.error instanceof Error ? stats.error.message : stats.error)}</span>}
          {s?.settled === false && <span className="text-mast-info">{tr("稳定中（刚改过设置，这一帧可能是旧设置拍的）", "Settling (settings just changed; this frame may use the old settings)")}</span>}
          <span>{tr("曝光", "Exposure")} {fmt(s?.exposureUs ?? null, 0, " µs")}</span>
          <span>{tr("增益", "Gain")} {fmt(s?.gainDb ?? null, 1, " dB")}</span>
          <span>{fmt(s?.fps ?? null, 1, " fps")}</span>
          <span>{tr("最大值", "Max")} {fmt(s?.max ?? null, 0)}</span>
          <span className={clsx((s?.saturatedFrac ?? 0) > 0 && "text-mast-warn")}>
            {tr("饱和", "Saturation")} {s?.saturatedFrac == null ? "—" : `${(s.saturatedFrac * 100).toFixed(2)} %`}
          </span>
          <span className="inline-flex items-center gap-1">
            <span aria-hidden className="inline-block h-2 w-2 rounded-full border-2 border-[#f97316]" />
            {tr("质心", "Centroid")} {s?.centroidPx ? `(${s.centroidPx.x.toFixed(1)}, ${s.centroidPx.y.toFixed(1)}) px` : "—"}
            {s?.centroidSensorPx && <span className="text-mast-faint">({tr("传感器", "sensor")} {s.centroidSensorPx.x.toFixed(1)}, {s.centroidSensorPx.y.toFixed(1)})</span>}
          </span>
          <span>
            FWHM {s?.fwhmPx ? `${s.fwhmPx.x.toFixed(1)} × ${s.fwhmPx.y.toFixed(1)} px` : "—"}
            {s?.fwhmMajorPx != null && s.fwhmMinorPx != null && (
              <span className="text-mast-faint">
                ({tr("长轴", "major")} {s.fwhmMajorPx.toFixed(1)} / {tr("短轴", "minor")} {s.fwhmMinorPx.toFixed(1)}
                {s.angleDeg != null ? `, ${s.angleDeg.toFixed(0)}°` : ""})
              </span>
            )}
          </span>
          {s?.roi && (
            <span>
              ROI {s.roi.w}×{s.roi.h} @ ({s.roi.x}, {s.roi.y}){s.roi.binning > 1 ? ` · ${tr("合并", "binning")} ${s.roi.binning}` : ""}
            </span>
          )}
          {s?.frameSeq != null && <span>{tr("帧", "Frame")} #{s.frameSeq}</span>}
        </div>

        {(expAct || gainAct) && (
          <div className="mt-3 flex flex-wrap items-center gap-4 border-t border-mast-border pt-2 text-xs">
            {expAct && (
              <label className="inline-flex items-center gap-1.5" title={expAct.hint ?? undefined}>
                <span className="text-mast-muted">{expAct.label}</span>
                <span className="inline-block w-24 [&_input]:w-full [&_input]:py-1">
                  <NumberField
                    value={exposure}
                    onChange={(v) => {
                      dirty.current.exposure = true;
                      setExposure(v);
                    }}
                  />
                </span>
                <span className="text-mast-faint">{inputParams(expAct)[0]?.unit ?? ""}</span>
              </label>
            )}
            {gainAct && (
              <label className="inline-flex items-center gap-1.5" title={gainAct.hint ?? undefined}>
                <span className="text-mast-muted">{gainAct.label}</span>
                <span className="inline-block w-24 [&_input]:w-full [&_input]:py-1">
                  <NumberField
                    value={gain}
                    onChange={(v) => {
                      dirty.current.gain = true;
                      setGain(v);
                    }}
                  />
                </span>
                <span className="text-mast-faint">{inputParams(gainAct)[0]?.unit ?? ""}</span>
              </label>
            )}
            <span className="text-[10.5px] text-mast-faint">{tr("停手 0.7 s 后生效", "Applied 0.7 s after you stop typing")}</span>
          </div>
        )}
        {others.length > 0 && (
          <div className="mt-2 space-y-1.5 border-t border-mast-border pt-2 text-xs">
            {others.map((a) => (
              <div key={a.id} className="flex flex-wrap items-center gap-1.5">
                {inputParams(a).map((p) => (
                  <span key={p.name} className="inline-flex items-center gap-1" title={p.description}>
                    <span className="text-[11px] text-mast-faint">{p.name}</span>
                    <span className="inline-block w-20 [&_input]:w-full [&_input]:py-1">
                      <NumberField
                        value={otherVals[`${a.id}|${p.name}`] ?? otherDefault(a, p.name, p.default)}
                        onChange={(v) => setOtherVals((m) => ({ ...m, [`${a.id}|${p.name}`]: v }))}
                      />
                    </span>
                    {p.unit && <span className="text-[11px] text-mast-faint">{p.unit}</span>}
                  </span>
                ))}
                <button
                  type="button"
                  disabled={!d.available}
                  onClick={() => {
                    const vals: Record<string, string> = {};
                    for (const p of inputParams(a)) {
                      const v = otherVals[`${a.id}|${p.name}`] ?? otherDefault(a, p.name, p.default);
                      if (v !== "") vals[p.name] = v;
                    }
                    trigger(a, vals);
                  }}
                  className="rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-2.5 py-1.5 text-sm text-mast-text hover:bg-mast-panel-2 disabled:opacity-50"
                  title={a.hint ?? a.skill}
                >
                  {a.label}
                </button>
              </div>
            ))}
          </div>
        )}
        <HintList hints={hints} />
      </Card>
    </div>
  );
}
