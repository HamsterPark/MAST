// ════════════════════════════════════════════════════════════════════════════
// feedbackSubject: which page a piece of feedback is about.
//
// The feedback float is shown on every page, so it has to work out where the user is.
// The agent used to be hard-coded, so all feedback was recorded as instrument_control,
// including feedback about the group chat, records and vision pages. That fails silently:
// the table reads normally but its contents are wrong.
//
// Same split as lib/ws.ts <-> hooks/useWsEvents.ts: no React, no JSX here, so
// node --test can run it (`frontend/test/feedbackSubject.test.ts`). Left inside
// FeedbackFloat.tsx the test could not import it (node does not strip JSX), and these
// checks are exactly the kind whose errors are invisible.
// ════════════════════════════════════════════════════════════════════════════

/**
 * Path prefix -> the surface the feedback is recorded against.
 *
 * `page_*` are UI surfaces, not LLM agents; the `agent` column of the feedback table is a
 * free-text label, so this widens its use without a schema change.
 *
 * After the tabs were merged, /builder /cognition /admin became /skills/builder
 * /records/memory /settings/admin. Renaming the strings is not enough: see
 * {@link subjectForPath} for why matching must use the longest prefix.
 *
 * Surfaces that had never been registered (monitoring, environment history, instrument
 * init, usage/cost, optics bench) are listed too; their feedback used to be recorded as
 * instrument_control.
 */
export const ROUTE_SUBJECT: { prefix: string; agent: string }[] = [
  { prefix: "/agents", agent: "orchestrator" },
  { prefix: "/qa", agent: "page_qa" },
  { prefix: "/skills/builder", agent: "page_builder" },
  { prefix: "/skills", agent: "page_skills" },
  { prefix: "/literature", agent: "literature" },
  { prefix: "/records/memory", agent: "page_cognition" },
  { prefix: "/records", agent: "page_records" },
  { prefix: "/wishlist", agent: "page_wishlist" },
  { prefix: "/settings/admin", agent: "page_admin" },
  { prefix: "/settings/setup", agent: "page_setup" },
  { prefix: "/settings/usage", agent: "page_usage" },
  { prefix: "/settings", agent: "page_settings" },
  { prefix: "/monitoring/env", agent: "page_env_history" },
  { prefix: "/monitoring", agent: "page_monitoring" },
  { prefix: "/experimental/optics", agent: "page_optics" },
  { prefix: "/experimental", agent: "page_experimental" },
];

/**
 * The surface that feedback on this path is recorded against.
 *
 * Longest prefix wins, not the first hit in the table. Each top-level tab has sub-pages,
 * and `/skills/builder` would otherwise match `/skills` first. Avoiding that by listing
 * long prefixes first would make row order load-bearing, and row order is easily
 * disturbed by an unrelated tidy-up with nothing to flag it.
 */
export function subjectForPath(pathname: string): string {
  let best: { prefix: string; agent: string } | null = null;
  for (const r of ROUTE_SUBJECT) {
    if (!pathname.startsWith(r.prefix)) continue;
    if (!best || r.prefix.length > best.prefix.length) best = r;
  }
  // "/" is the instrument Chat page — a real private chat with the IC agent.
  return best ? best.agent : "instrument_control";
}
