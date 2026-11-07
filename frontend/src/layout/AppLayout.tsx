import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import clsx from "clsx";
import { applyTheme, useUiStore } from "@/store";
import { useAutonomyMode } from "@/hooks/useAutonomyMode";
import { useOpenFetchRequests } from "@/hooks/useOpenFetchRequests";
import { useOpenAgentRequests } from "@/hooks/useOpenAgentRequests";
import { useOpenPromptFeed } from "@/api/operator";
import { OperatorAlertHost } from "@/components/operator/OperatorAlertHost";
import { wishlistBadge, wishlistBadgeLabel } from "@/lib/operatorPrompts";
import { TopBar } from "@/components/shell/TopBar";
import { NoSampleBanner } from "@/components/scope/ScopeControls";
import { SetupBanner } from "@/components/shell/SetupBanner";
import { RightPanel } from "@/components/shell/RightPanel";
import { FeedbackFloat } from "@/components/chat/FeedbackFloat";
import { OnboardingGuide } from "@/components/shell/OnboardingGuide";
import { useLegacyFeatures } from "@/hooks/useLegacyFeatures";
import { tr } from "@/lib/i18n";
import { homeLabel, statusMarker, visibleTabs } from "@/lib/nav";

const RAIL_COLLAPSED_KEY = "mast.rightRail.collapsed";

/** A count on a nav tab: N things are waiting for the operator there. `urgent` turns it red
 *  (a safety prompt is among them). */
function PendingBadge({ n, label, urgent = false }: { n: number; label: string; urgent?: boolean }) {
  return (
    <span
      className={clsx(
        `ml-1.5 inline-flex min-w-[18px] items-center justify-center
         rounded-full px-1.5 py-0.5 text-[11px] font-semibold leading-none text-white`,
        urgent ? "bg-mast-danger" : "bg-mast-accent",
      )}
      title={label}
      aria-label={label}
    >
      {n > 99 ? "99+" : n}
    </span>
  );
}

// App shell: header (TopBar) + [ top tabs and page content | right status panel ].
// The tab list comes from lib/nav.ts, filtered by the legacy-features setting.

export default function AppLayout() {
  const theme = useUiStore((s) => s.theme);
  const activeConversationId = useUiStore((s) => s.activeConversationId);
  const { enabled: legacy } = useLegacyFeatures();
  const tabs = visibleTabs(legacy);
  // Global operating mode → a whole-app colored border: safe=green, semi=amber,
  // auto=none. Rendered as an INSET ring (box-shadow) so it never shifts layout
  // or clips at the viewport edge, and stays put across navigation (AppLayout
  // never unmounts).
  const { mode } = useAutonomyMode();
  // Open literature fetch requests and agent requests, shown as counts on their
  // tabs so the operator notices them. Degrades to 0 (no badge).
  const openFetchRequests = useOpenFetchRequests();
  const openAgentRequests = useOpenAgentRequests();
  // Structured operator prompts (an agent asked a person to do or confirm something).
  // Polled every few seconds, also while this tab is in the background, because a person
  // at the instrument is looking at the vendor software, not at a browser tab. This is
  // the one place that polls; the alert host below and the page read the same cache.
  // Only an optical station has them: on an STM station (or before the service has said
  // what it is) the feed is empty and `enabled` is false, nothing is requested, the tab
  // badge stays what the free-text requests make it, and the alert host is not mounted.
  const operatorPrompts = useOpenPromptFeed();
  const wishlistCount = wishlistBadge(operatorPrompts.openCount, openAgentRequests);
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  // Right rail collapse, persisted in localStorage.
  const [railCollapsed, setRailCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(RAIL_COLLAPSED_KEY) === "1";
    } catch {
      return false;
    }
  });
  const toggleRail = () => {
    setRailCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem(RAIL_COLLAPSED_KEY, next ? "1" : "0");
      } catch {
        /* private mode / no storage — collapse still works for the session */
      }
      return next;
    });
  };

  return (
    <div className="flex h-screen flex-col bg-mast-bg text-mast-text">
      <TopBar />
      {/* Shown while no sample is selected: data-producing actions are refused,
          everything else keeps working. Informational, never blocking. */}
      <NoSampleBanner />
      {/* Instrument setup incomplete or hardware changed: a persistent banner,
          deliberately not a modal, so E-STOP stays reachable. */}
      <SetupBanner />
      <div className="flex min-h-0 flex-1">
        {/* left: top-tabs + content */}
        <div className="flex min-w-0 flex-1 flex-col">
          {/* `flex-wrap`: on narrow windows the tabs wrap rather than scroll
              or collapse into a dropdown. */}
          <nav className="flex flex-wrap items-center gap-x-0.5 border-b border-mast-border bg-mast-panel px-3">
            {tabs.map((t) => (
              <NavLink
                key={t.to}
                to={t.to}
                end={t.end}
                className={({ isActive }) =>
                  clsx(
                    "-mb-px whitespace-nowrap border-b-2 px-[13px] py-[11px] text-[13px]",
                    isActive
                      ? "border-mast-accent bg-mast-accent-soft font-semibold text-mast-accent"
                      : "border-transparent text-mast-muted hover:text-mast-text",
                  )
                }
              >
                {t.to === "/" ? homeLabel(legacy) : t.label}
                {statusMarker(t) && (
                  <span
                    className={clsx(
                      "ml-1 rounded-mast-badge border px-1 text-[10px] font-normal",
                      t.status === "incomplete"
                        ? "border-mast-warn text-mast-warn"
                        : "border-mast-border text-mast-faint",
                    )}
                  >
                    {statusMarker(t)}
                  </span>
                )}
                {t.to === "/literature" && openFetchRequests > 0 && (
                  <PendingBadge
                    n={openFetchRequests}
                    label={tr(`${openFetchRequests} 条取文请求等待处理`, `${openFetchRequests} fetch requests waiting`)}
                  />
                )}
                {t.to === "/wishlist" && wishlistCount > 0 && (
                  <PendingBadge
                    n={wishlistCount}
                    urgent={operatorPrompts.hasSafety}
                    label={wishlistBadgeLabel(operatorPrompts.openCount, openAgentRequests, operatorPrompts.hasSafety)}
                  />
                )}
              </NavLink>
            ))}
          </nav>
          <main className="min-h-0 flex-1 overflow-auto">
            {/* Full-width content fills the left column;
                pages cap their own width where needed. */}
            <div className="p-5">
              <Outlet />
            </div>
          </main>
        </div>
        {/* right: persistent instrument/environment/experiment status rail.
            Collapsible: the thin handle at its left edge toggles it. */}
        {railCollapsed ? (
          <div className="hidden w-8 shrink-0 flex-col items-center border-l border-mast-border bg-mast-panel lg:flex">
            <button
              type="button"
              onClick={toggleRail}
              title={tr("展开侧栏", "Expand side panel")}
              aria-label={tr("展开侧栏", "Expand side panel")}
              aria-expanded={false}
              className="flex h-full w-full flex-col items-center gap-2 py-3 text-mast-muted hover:bg-mast-bg hover:text-mast-text"
            >
              <span className="text-sm leading-none">‹</span>
              <span
                className="mt-1 text-[10px] font-semibold uppercase tracking-wider"
                style={{ writingMode: "vertical-rl" }}
              >
                {tr("状态栏", "Status")}
              </span>
            </button>
          </div>
        ) : (
          <div className="hidden lg:flex">
            <button
              type="button"
              onClick={toggleRail}
              title={tr("收起侧栏", "Collapse side panel")}
              aria-label={tr("收起侧栏", "Collapse side panel")}
              aria-expanded={true}
              className="w-3 shrink-0 cursor-pointer border-l border-mast-border bg-mast-panel text-mast-muted hover:bg-mast-bg hover:text-mast-text"
            >
              <span className="text-xs leading-none">›</span>
            </button>
            <RightPanel />
          </div>
        )}
      </div>
      {/* Feedback float: the operator's way to note a problem on any page, so it is
          not tied to the legacy built-in agents. Portals to <body>; its conversation
          id (null without a chat) comes from the UI store. */}
      <FeedbackFloat conversationId={activeConversationId} />
      <OnboardingGuide />
      {/* New operator prompt: toast, beep, desktop notification, count in the tab title.
          Portal-free fixed layer, above the operating-mode frame, never blocks clicks.
          Optical stations only: an STM station gets none of it, not even the listeners. */}
      {operatorPrompts.enabled && <OperatorAlertHost feed={operatorPrompts} />}
      {/* Global operating-mode frame — a FULL-VIEWPORT fixed border overlaying the
          whole screen (safe=green, semi=amber). fixed+inset-0+high z + no
          pointer events, so no panel/background can paint over it (the old
          ring-inset got covered by the TopBar/panels) and it never clips or
          scrolls. */}
      {mode === "safe" && (
        <div
          aria-hidden
          className="pointer-events-none fixed inset-0 z-[9999] border-4 border-mast-auto"
        />
      )}
      {mode === "semi" && (
        <div
          aria-hidden
          className="pointer-events-none fixed inset-0 z-[9999] border-4 border-mast-warn"
        />
      )}
    </div>
  );
}
