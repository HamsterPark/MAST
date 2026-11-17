import { useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import { useInstrumentInit } from "@/hooks/useInstrumentInit";
import { useStationProfile } from "@/hooks/useStationProfile";
import { firstRunBannerShown, profileLabel, type StationProfile } from "@/lib/stationProfile";
import { tr } from "@/lib/i18n";

// ── First use: which kind of station is this? ────────────────────────────────
//
// Shown on a brand-new installation only: nobody has chosen a station profile (nothing is
// stored and no environment variable is set, `station_profile_source: "default"`) AND the
// machine has never been set up (the instrument-init record has no completion, see
// initHistory). Until a profile is chosen the machine runs as an STM station by default.
// That is right for the STM labs MAST started in and wrong for anyone else: it dials
// Nanonis, may probe the serial ports and shows tip readouts.
//
// An STM station that has been running for months also has no stored profile after an
// upgrade; it has been set up, so it gets no banner (its interface must not change, and it
// may well have no admin PIN either). Neither does a machine whose record cannot be read.
// The Settings card shows the same two steps whenever no profile was chosen, without this
// restriction: there they disturb nobody.
//
// Two things are needed, in this order, because the profile is a PIN-protected setting and
// there is no default PIN:
//   1. an admin PIN;
//   2. the choice, saved through the Settings card's confirmation.
// The banner names both, says whether the PIN exists, and links to the card. Like the setup
// banner it is never a modal (the E-STOP must stay reachable), and "Later" folds it for this
// session only.
//
// Not on the Settings page itself: the card there shows the same steps next to the choice.
// Never while an environment variable pins the profile: the variable is the choice.

/** The station-profile card (SettingsPage scrolls to the hash). */
export const STATION_CARD_PATH = "/settings/general#station-profile";

/** Same query key as the PIN gate and the confirmation, so they share one answer. */
function usePinIsSet(): boolean | undefined {
  const q = useQuery({
    queryKey: ["admin", "pin-status"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/admin/pin-status");
      if (error) throw error;
      return data;
    },
  });
  return q.data?.pin_is_set;
}

/** The two first-use steps, with the PIN step's state. Mounted only on first use. */
export function StationSetupSteps({ where, profile }: { where: "card" | "banner"; profile: StationProfile }) {
  const pinIsSet = usePinIsSet();
  return (
    <div className={where === "card" ? "mt-2 rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-xs text-mast-warn" : "mt-1 text-xs"}>
      {where === "card" && (
        <p className="font-semibold">
          {tr(
            `第一次使用：这台机器还没确认站点类型，现在按默认的「${profileLabel(profile)}」运行。`,
            `First use: this machine's station type has not been chosen yet; it runs as the default "${profileLabel(profile)}".`,
          )}
        </p>
      )}
      <ol className="mt-1 list-decimal space-y-0.5 pl-5" data-testid="station-setup-steps">
        <li>
          {tr("管理员 PIN：", "Admin PIN: ")}
          {pinIsSet === true ? (
            <span className="font-semibold" data-step-done="pin">
              {tr("已设置 ✓", "set ✓")}
            </span>
          ) : pinIsSet === false ? (
            <span data-step-open="pin">
              {tr("还没有，确认框里可以直接设，或到", "not set yet; set it in the confirmation, or under")}
              <Link to="/settings/admin" className="mx-1 underline">
                {tr("设置 → 高级", "Settings → Advanced")}
              </Link>
            </span>
          ) : null}
        </li>
        <li>
          {where === "card" ? (
            tr(
              "在上面选这台机器是哪种站（当前这项也算），确认框里输入 PIN 保存。",
              "Choose above which kind of station this is (the current one counts too) and save it with the PIN.",
            )
          ) : (
            <>
              {tr("选择站点类型：", "Choose the station type: ")}
              <Link to={STATION_CARD_PATH} className="font-semibold underline">
                {tr("去选择 →", "Choose →")}
              </Link>
            </>
          )}
        </li>
      </ol>
    </div>
  );
}

export function StationSetupBanner() {
  const [collapsed, setCollapsed] = useState(false);
  const loc = useLocation();
  const station = useStationProfile();
  // The setup record is asked for only while no profile was chosen (on an STM station the
  // setup banner shares this very request).
  const init = useInstrumentInit({ enabled: station.firstRun });
  // All hooks above run on every render; only now may the component bail out.
  if (!firstRunBannerShown({ neverChosen: station.firstRun, init: init.data, initFailed: init.isError })) return null;
  if (loc.pathname.startsWith("/settings/general")) return null;

  const label = profileLabel(station.profile);

  if (collapsed) {
    return (
      <button
        type="button"
        onClick={() => setCollapsed(false)}
        className="flex w-full items-center gap-2 border-b border-mast-warn-border bg-mast-warn-bg px-4 py-1 text-left text-xs text-mast-warn"
      >
        <span>●</span>
        <span>{tr("站点类型还没确认", "Station type not chosen yet")}</span>
        <span className="ml-auto underline">{tr("展开", "Expand")}</span>
      </button>
    );
  }

  return (
    <div
      className="border-b border-mast-warn-border bg-mast-warn-bg px-4 py-2.5 text-sm text-mast-warn"
      role="status"
      data-testid="station-setup-banner"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <b>
          {tr(
            `这台机器还没确认站点类型，现在按默认的「${label}」运行`,
            `This machine's station type has not been chosen yet; it runs as the default "${label}"`,
          )}
        </b>
        <div className="ml-auto flex items-center gap-2">
          <Link
            to={STATION_CARD_PATH}
            className="rounded-mast-ctl border border-current px-2.5 py-1 text-xs font-semibold hover:opacity-80"
          >
            {tr("去选择 →", "Choose →")}
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
      <StationSetupSteps where="banner" profile={station.profile} />
    </div>
  );
}
