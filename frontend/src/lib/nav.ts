// Top-bar navigation table: the only place a page becomes reachable without
// knowing its URL.
//
// It lives apart from AppLayout so it can be asserted against. A page missing
// from here still routes, renders and type-checks, and looks fine in a screenshot
// of itself; the only symptom is that nobody can get to it. test/nav.test.ts
// checks that every section has a component and every old address redirects.
//
// Big tabs group related pages. Second-level pages are real route segments, not
// in-page state, so bookmarks and shared links land on the right page. Old
// top-level addresses redirect to their new place, see LEGACY_REDIRECTS.
//
// Feature status:
//   - `legacy`: hidden unless Settings → General → Legacy features is on (the
//     built-in agent stack). The route still exists and explains how to enable it.
//   - `status: "incomplete"`: shown, with a marker that the feature is unfinished.

import { tr } from "./i18n.ts";

export type FeatureStatus = "incomplete";

export interface NavSection {
  /** Second-level path segment (appended to the tab's `to`). */
  seg: string;
  label: string;
  legacy?: boolean;
  status?: FeatureStatus;
}

export interface NavTab {
  to: string;
  label: string;
  /** react-router `end`: match this path exactly, not as a prefix. */
  end?: boolean;
  /**
   * Second-level pages of this tab.
   *
   * A tab with sections renders nothing at its own `to`; it forwards to a section
   * (the last one visited, else the first). A tab without sections is one page.
   */
  sections?: NavSection[];
  legacy?: boolean;
  status?: FeatureStatus;
}

export const TABS: NavTab[] = [
  // "/" is the built-in agent chat when legacy features are on, and the
  // instrument overview otherwise (see HomeRoute in router.tsx).
  { to: "/", label: tr("仪器 Chat", "Instrument Chat"), end: true },
  { to: "/agents", label: "Agents", legacy: true },
  { to: "/qa", label: tr("查询助手", "Query Assistant"), legacy: true },
  {
    to: "/skills",
    label: tr("技能", "Skills"),
    sections: [
      { seg: "library", label: tr("技能目录", "Skill Library") },
      { seg: "builder", label: tr("技能构建器", "Skill Builder") },
    ],
  },
  { to: "/literature", label: tr("文献库", "Literature"), status: "incomplete" },
  // Spectroscopy / TERS: the spectrometer PC runs a satellite and MAST on the STM
  // PC is the hub. It has its own tab because it stays open during an experiment.
  { to: "/ters", label: tr("光谱 / TERS", "Spectra / TERS") },
  // Records, data gallery, multi-day conduct and memory: a conduct run is always
  // bound to an experiment, so it belongs with the records.
  {
    to: "/records",
    label: tr("实验记录", "Records"),
    sections: [
      { seg: "log", label: tr("记录", "Log") },
      { seg: "gallery", label: tr("数据图库", "Data Gallery") },
      { seg: "map", label: tr("扫描地图", "Scan Map") },
      { seg: "conduct", label: tr("多天 conduct", "Multi-day Conduct"), legacy: true },
      { seg: "memory", label: tr("记忆", "Memory"), legacy: true },
    ],
  },
  // Requests from agents (including external agents via mast_ask_operator) wait
  // here for the operator, so the page stays visible; only its wishlist/feedback
  // form is legacy.
  { to: "/wishlist", label: tr("操作员请求", "Operator Requests") },
  // Settings, instrument setup, advanced administration and usage. The group root
  // stays `/settings` because it is the most bookmarked address. Instrument setup
  // keeps a permanent place because the banner that links to it disappears once
  // setup is complete.
  {
    to: "/settings",
    label: tr("设置", "Settings"),
    sections: [
      { seg: "general", label: tr("常规设置", "General") },
      { seg: "setup", label: tr("仪器初始化", "Instrument Setup") },
      { seg: "admin", label: tr("高级管理 🔒", "Advanced 🔒") },
      { seg: "usage", label: tr("用量花销", "Usage & Cost"), legacy: true },
    ],
  },
  {
    to: "/monitoring",
    label: tr("监控", "Monitoring"),
    sections: [
      { seg: "current", label: tr("电流监控", "Current Monitor") },
      { seg: "env", label: tr("环境历史", "Environment History") },
    ],
  },
  {
    to: "/experimental",
    label: tr("实验性功能", "Experimental"),
    sections: [
      { seg: "tools", label: tr("仪器工具", "Instrument Tools") },
      { seg: "optics", label: tr("光学台", "Optics Bench") },
      // Drawn optical path of the setup, shared with the AI. Not legacy and not tied to a
      // station profile: an STM station with a TERS path and the optical station both have
      // an optical layout to describe.
      { seg: "layout", label: tr("光路图", "Optical Layout") },
    ],
  },
];

/** Label of the "/" tab for the current legacy setting. */
export function homeLabel(legacy: boolean): string {
  return legacy ? tr("仪器 Chat", "Instrument Chat") : tr("总览", "Overview");
}

/** Tabs and sections visible for the given legacy setting. */
export function visibleTabs(legacy: boolean): NavTab[] {
  return TABS.filter((t) => legacy || !t.legacy).map((t) =>
    t.sections ? { ...t, sections: t.sections.filter((s) => legacy || !s.legacy) } : t,
  );
}

/** Short marker appended to a label for its feature status. */
export function statusMarker(t: { legacy?: boolean; status?: FeatureStatus }): string | null {
  if (t.status === "incomplete") return tr("不完善", "incomplete");
  if (t.legacy) return tr("旧版", "legacy");
  return null;
}

/**
 * Old top-level addresses and where those pages live now.
 *
 * Plain data so that router.tsx can build the redirects from it and the tests can
 * assert that every entry points at a real page. Entries are only ever added:
 * removing one breaks somebody's bookmark with no clue why.
 */
export const LEGACY_REDIRECTS: Record<string, string> = {
  "/builder": "/skills/builder",
  "/cognition": "/records/memory",
  "/admin": "/settings/admin",
  "/setup": "/settings/setup",
  "/usage": "/settings/usage",
  "/env-history": "/monitoring/env",
  "/optics": "/experimental/optics",
};

/**
 * Pages that are reachable by URL on purpose and deliberately have no tab.
 *
 * An explicit list, so that "no tab" is always a decision someone wrote down
 * rather than an omission nobody noticed. `/dashboard` is the home page when
 * legacy features are off and is linked from the right panel; `/chat` is an alias
 * of `/`; `/vision` (legacy) is linked from the top of the chat page's vision
 * buffer view, which test/scanMapPairing.test.ts pins.
 */
export const INTENTIONALLY_UNLISTED = ["/dashboard", "/vision", "/chat"] as const;

/** Full path of a section under a tab. */
export function sectionPath(tab: NavTab, seg: string): string {
  return `${tab.to === "/" ? "" : tab.to}/${seg}`;
}

/** All section ids of a tab (the allowed list for useStickyTab). */
export function sectionIds(tab: NavTab): string[] {
  return (tab.sections ?? []).map((s) => s.seg);
}

/** A top-bar tab, looked up by `to`. */
export function findTab(to: string): NavTab | undefined {
  return TABS.find((t) => t.to === to);
}
