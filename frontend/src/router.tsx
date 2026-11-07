import type { ReactElement } from "react";
import { createBrowserRouter } from "react-router-dom";
import AppLayout from "@/layout/AppLayout";
import { GroupIndex, LegacyRedirect, TabGroup } from "@/layout/TabGroup";
import { LEGACY_REDIRECTS, TABS, sectionPath } from "@/lib/nav";
import ChatPage from "@/pages/ChatPage";
import AgentsPage from "@/pages/AgentsPage";
import QaPage from "@/pages/QaPage";
import SkillsPage from "@/pages/SkillsPage";
import BuilderPage from "@/pages/BuilderPage";
import CognitionPage from "@/pages/CognitionPage";
import LiteraturePage from "@/pages/LiteraturePage";
import RecordsPage from "@/pages/RecordsPage";
import GalleryPage from "@/pages/GalleryPage";
import ConductPage from "@/pages/ConductPage";
import WishlistPage from "@/pages/WishlistPage";
import AdminPage from "@/pages/AdminPage";
import SettingsPage from "@/pages/SettingsPage";
import SetupPage from "@/pages/SetupPage";
import ExperimentalPage from "@/pages/ExperimentalPage";
import DashboardPage from "@/pages/DashboardPage";
import VisionPage, { ScanMapSection } from "@/pages/VisionPage";
import OpticsPage from "@/pages/OpticsPage";
import OpticalLayoutPage from "@/pages/OpticalLayoutPage";
import UsagePage from "@/pages/UsagePage";
import MonitoringPage from "@/pages/MonitoringPage";
import EnvHistoryPage from "@/pages/EnvHistoryPage";
import TersPage from "@/pages/TersPage";
import { LegacyOnly } from "@/components/shell/LegacyOnly";
import { useLegacyFeatures } from "@/hooks/useLegacyFeatures";

/** "/": the built-in agent chat with legacy features on, the overview otherwise. */
function HomeRoute() {
  const { enabled, loaded } = useLegacyFeatures();
  if (!loaded) return null;
  return enabled ? <ChatPage /> : <DashboardPage />;
}

// Route structure is generated from the table in lib/nav.ts, so paths have one
// source of truth; this file only maps each path to its component. Old top-level
// addresses redirect with their query string (see LEGACY_REDIRECTS).
//
// Legacy pages keep their routes but render through <LegacyOnly>, which explains
// how to enable them while legacy features are off.

/**
 * Full path of every section → the component that renders it.
 *
 * Keyed by the full path from `sectionPath()`, not the segment, because segments
 * can repeat across tabs. Exported for the tests: a section added to nav.ts
 * without a component here would render an empty page.
 */
export const SECTION_ELEMENTS: Record<string, ReactElement> = {
  "/skills/library": <SkillsPage />,
  "/skills/builder": <BuilderPage />,

  "/records/log": <RecordsPage />,
  "/records/gallery": <GalleryPage />,
  "/records/map": <ScanMapSection />,
  "/records/conduct": <ConductPage />,
  "/records/memory": <CognitionPage />,

  "/settings/general": <SettingsPage />,
  "/settings/setup": <SetupPage />,
  "/settings/admin": <AdminPage />,
  "/settings/usage": <UsagePage />,

  "/monitoring/current": <MonitoringPage />,
  "/monitoring/env": <EnvHistoryPage />,

  "/experimental/tools": <ExperimentalPage />,
  "/experimental/optics": <OpticsPage />,
  "/experimental/layout": <OpticalLayoutPage />,
};

/** Tabs without sections, and pages deliberately without a tab. */
const FLAT_ELEMENTS: Record<string, ReactElement> = {
  "/agents": <AgentsPage />,
  "/qa": <QaPage />,
  "/literature": <LiteraturePage />,
  "/ters": <TersPage />,
  "/wishlist": <WishlistPage />,
  // reachable, non-tab (see INTENTIONALLY_UNLISTED):
  "/dashboard": <DashboardPage />,
  "/vision": <VisionPage />,
  "/chat": <ChatPage />,
};

const strip = (p: string) => p.replace(/^\//, "");

/** Tabs with sections → a group route with children, derived from TABS. */
const groupRoutes = TABS.filter((t) => t.sections?.length).map((t) => ({
  path: strip(t.to),
  element: <TabGroup to={t.to} />,
  children: [
    { index: true, element: <GroupIndex to={t.to} /> },
    ...(t.sections ?? []).map((sec) => {
      const el = SECTION_ELEMENTS[sectionPath(t, sec.seg)];
      return { path: sec.seg, element: sec.legacy ? <LegacyOnly>{el}</LegacyOnly> : el };
    }),
  ],
}));

/** Old top-level addresses, so bookmarks and links keep working. */
const legacyRoutes = Object.entries(LEGACY_REDIRECTS).map(([from, to]) => ({
  path: strip(from),
  element: <LegacyRedirect to={to} />,
}));

/** Flat pages that belong to the legacy built-in agent stack. */
const LEGACY_FLAT = new Set(["/agents", "/qa", "/vision", "/chat"]);

const flatRoutes = Object.entries(FLAT_ELEMENTS).map(([p, element]) => ({
  path: strip(p),
  element: LEGACY_FLAT.has(p) ? <LegacyOnly>{element}</LegacyOnly> : element,
}));

export const router = createBrowserRouter([
  {
    path: "/",
    element: <AppLayout />,
    children: [
      { index: true, element: <HomeRoute /> },
      ...flatRoutes,
      ...groupRoutes,
      ...legacyRoutes,
    ],
  },
]);
