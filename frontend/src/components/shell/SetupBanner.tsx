import { useState } from "react";
import { Link, useLocation } from "react-router-dom";
import clsx from "clsx";
import { useInstrumentInit } from "@/hooks/useInstrumentInit";
import { useStationProfile } from "@/hooks/useStationProfile";
import { SETUP_PATH, isSetupPath } from "@/lib/setupNav";
import { tr } from "@/lib/i18n";

// ── New-instrument setup prompt ──────────────────────────────────────────────
//
// Deliberately a persistent banner, not a modal.
//
// The UI must never freeze: a full-screen modal that has to be completed before it
// can be dismissed is a hazard when the user just wants to press the emergency stop.
// So it is prominent, clickable, and collapsible to one line, but does not go away
// until the required items are filled. The collapsed state lives only in this
// session (useState, not persisted), so a page refresh brings it back.
//
// When it appears (the criteria live in the backend GET /api/instrument-init):
//   * a required item has no answer (the authoritative, content-only criterion:
//     computed from the values themselves, not from any marker, so it is unaffected
//     by which file an upgrade overwrote);
//   * the hardware fingerprint differs from the last completion (different machine
//     or recalibrated);
//   * a completion stamp was never written.
//
// When the state cannot be read (degraded) it is not shown: an unreadable checklist
// that presents as a red warning gets ignored within a week, which defeats it when a
// value really is missing.
//
// It is never shown on an optical station (station profile "optical"): that machine
// has no STM, so the required items below (preamp gain, bias polarity, safety
// envelope, approach preset, ...) do not apply to it and the banner would nag about
// quantities that do not exist there.

export function SetupBanner() {
  const [collapsed, setCollapsed] = useState(false);
  const loc = useLocation();
  const q = useInstrumentInit();
  const station = useStationProfile();
  const d = q.data;

  // Do not nag when the user is already on the setup page.
  //
  // The setup page moved from /setup to /settings/setup and the old path is now a
  // redirect. The path check is not hard-coded: it is derived from LEGACY_REDIRECTS
  // in nav.ts (see lib/setupNav.ts), so it does not depend on an assumption that
  // lives in router.tsx.
  if (isSetupPath(loc.pathname)) return null;
  // All hooks above run on every render; only now may the component bail out.
  if (station.isOptical) return null;
  if (q.isLoading || q.error || !d || d.degraded) return null;
  if (!d.should_prompt) return null;

  const req = d.counts?.required ?? { total: 0, complete: 0 };
  const missing = req.total - req.complete;
  const urgent = d.needs_setup;

  if (collapsed) {
    return (
      <button
        type="button"
        onClick={() => setCollapsed(false)}
        className={clsx(
          "flex w-full items-center gap-2 border-b px-4 py-1 text-left text-xs",
          urgent
            ? "border-mast-danger-border bg-mast-danger-bg text-mast-danger"
            : "border-mast-warn-border bg-mast-warn-bg text-mast-warn",
        )}
      >
        <span>●</span>
        <span>
          {urgent
            ? tr(`新仪器初始化：还有 ${missing} 项必填没填`, `New-instrument setup: ${missing} required items still empty`)
            : tr("新仪器初始化：建议过一遍", "New-instrument setup: review recommended")}
        </span>
        <span className="ml-auto underline">{tr("展开", "Expand")}</span>
      </button>
    );
  }

  return (
    <div
      className={clsx(
        "border-b px-4 py-2.5 text-sm",
        urgent
          ? "border-mast-danger-border bg-mast-danger-bg text-mast-danger"
          : "border-mast-warn-border bg-mast-warn-bg text-mast-warn",
      )}
      role="status"
    >
      {/* One sentence, no subtitle: a reader who sees the banner does not need to be
          convinced it should exist; they need the count and the button. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <b>
          {urgent
            ? tr(`这台仪器还有 ${missing} 项必填参数没有确定`, `This instrument still has ${missing} required parameters undefined`)
            : d.fingerprint_changed
              ? tr("硬件指纹变了 —— 这可能是另一台仪器", "Hardware fingerprint changed: this may be a different instrument")
              : tr("这台仪器还没走过初始化", "This instrument has not been set up yet")}
        </b>
        <div className="ml-auto flex items-center gap-2">
          <Link
            to={SETUP_PATH}
            className="rounded-mast-ctl border border-current px-2.5 py-1 text-xs font-semibold hover:opacity-80"
          >
            {tr("去填 →", "Fill in →")}
          </Link>
          <button
            type="button"
            onClick={() => setCollapsed(true)}
            className="text-xs underline opacity-80 hover:opacity-100"
            title={tr("折起来（只在这次会话里生效，刷新就回来）", "Collapse (this session only; returns on refresh)")}
          >
            {tr("稍后", "Later")}
          </button>
        </div>
      </div>
    </div>
  );
}
