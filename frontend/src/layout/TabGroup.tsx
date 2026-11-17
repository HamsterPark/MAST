// TabGroup: the shell of a big tab that groups several pages. A second-level tab
// strip plus <Outlet/>.
//
// Second-level pages are real route segments, not in-page state, so bookmarks and
// shared links land on the right page. The remembered section only decides where
// a bare group address (`/settings`) goes.
//
// Legacy sections are left out of the strip while legacy features are off; their
// routes stay and show how to enable them. Sections that need a station capability
// this station lacks (`requires`) are left out the same way; their routes stay and
// say which station they belong to (CapabilityOnly).

import { useEffect } from "react";
import { Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import { SubTabs } from "@/components/controls";
import { useStickyTab } from "@/hooks/useStickyTab";
import { useLegacyFeatures } from "@/hooks/useLegacyFeatures";
import { useStationCapabilities } from "@/hooks/useStationCapabilities";
import { sectionPath, statusMarker, visibleTabs, type NavTab } from "@/lib/nav";

/**
 * Which section of this tab the URL is on, computed from the pathname. The child
 * routes are literal paths, not `:section`, so `useParams()` has nothing to give.
 */
function currentSection(pathname: string, to: string, ids: string[]): string | null {
  const base = to === "/" ? "" : to;
  if (!pathname.startsWith(`${base}/`)) return null;
  const seg = pathname.slice(base.length + 1).split("/")[0] ?? "";
  return ids.includes(seg) ? seg : null;
}

function useVisibleTab(to: string): NavTab | undefined {
  const { enabled } = useLegacyFeatures();
  const station = useStationCapabilities();
  return visibleTabs(enabled, station.has).find((t) => t.to === to);
}

/**
 * The shell of one big tab. `to` is its `to` in TABS.
 *
 * Route shape (see router.tsx)::
 *
 *     { path: "settings", element: <TabGroup to="/settings" />, children: [
 *         { index: true, element: <GroupIndex to="/settings" /> },
 *         { path: "general", element: <SettingsPage /> },
 *         … ] }
 */
export function TabGroup({ to }: { to: string }) {
  const tab = useVisibleTab(to);
  const nav = useNavigate();
  const { pathname } = useLocation();
  const ids = (tab?.sections ?? []).map((s) => s.seg);
  const section = currentSection(pathname, to, ids);
  const [, remember] = useStickyTab(`group.${to}`, ids, ids[0] ?? "");

  // Remember the section actually shown, including when it was reached from a
  // bookmark rather than a click.
  useEffect(() => {
    if (section) remember(section);
  }, [section, remember]);

  if (!tab || !tab.sections?.length) {
    // Not in the nav table or without sections: should not happen. Do not render
    // a blank page or throw; go to the home page.
    return <Navigate to="/" replace />;
  }

  return (
    <div className="space-y-4">
      <SubTabs
        tabs={tab.sections.map((s) => {
          const mark = statusMarker(s);
          return { id: s.seg, label: mark ? `${s.label} · ${mark}` : s.label };
        })}
        value={section ?? ""}
        onChange={(seg) => nav(sectionPath(tab, seg))}
      />
      <Outlet />
    </div>
  );
}

/**
 * Where a bare group address goes: the last visited section, else the first.
 * `replace`, so the forwarding address does not break the Back button.
 *
 * It waits until the station has said what it has: before that every section counts as
 * present, and `/monitoring` would forward a generic station to the signal monitor it
 * does not have. The remembered section is checked against the visible ones at that
 * moment, not when the remembered value was first read (useStickyTab reads it once).
 */
export function GroupIndex({ to }: { to: string }) {
  const tab = useVisibleTab(to);
  const station = useStationCapabilities();
  const ids = (tab?.sections ?? []).map((s) => s.seg);
  const [remembered] = useStickyTab(`group.${to}`, ids, ids[0] ?? "");
  const { search, hash } = useLocation();
  if (!station.ready) return null;
  if (!tab || !ids.length) return <Navigate to="/" replace />;
  const target = ids.includes(remembered) ? remembered : ids[0]!;
  return <Navigate to={`${sectionPath(tab, target)}${search}${hash}`} replace />;
}

/**
 * An address from before the tabs were grouped. Forwards to where the page is
 * now, keeping `search` and `hash` (deep links such as the monitor's `?seg=`).
 */
export function LegacyRedirect({ to }: { to: string }) {
  const { search, hash } = useLocation();
  return <Navigate to={`${to}${search}${hash}`} replace />;
}

export type { NavTab };
