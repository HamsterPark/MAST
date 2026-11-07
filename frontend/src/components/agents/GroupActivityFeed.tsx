import { useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Badge, Card, DegradedNote, EmptyNote, ErrorNote, Spinner } from "@/components/ui";
import { useWsEvent } from "@/hooks/useWsEvents";
import { langTag, tr } from "@/lib/i18n";
import { transcriptCursorFor, type TranscriptEventData } from "@/lib/transcriptRefresh";
import { Avatar, agentLabel } from "./registry";
import { parseRowMeta, useRunTaskStore } from "./runTaskStore";

// ════════════════════════════════════════════════════════════════════════════
// GroupActivityFeed — an agent's messages from group-chat (multi-agent orchestrator)
// runs, surfaced READ-ONLY in its own view. Two sources, merged:
//   • LIVE: the in-progress run's messages straight from the module-scoped
//     run-task store (real-time — no 5s poll lag and no waiting for the
//     agent-boundary persist flush, which would show tool calls only once the
//     agent "spoke");
//   • DURABLE: the persisted transcript across all past runs
//     (GET /api/agents/{id}/group-activity, polled).
// Deduped by a content signature so a resume-replayed row (same text, new seq)
// folds into one, and so a live row already persisted isn't shown twice.
// ════════════════════════════════════════════════════════════════════════════

type Row = {
  key: string;
  role: string;
  text: string;
  t: number;
  title?: string;
  live?: boolean;
  // Tool rows show a one-line summary; the detail it drops expands on click.
  // Absent for rows written before the sidecar existed.
  tool?: string;
  args?: string;
  argsClipped?: boolean;
  detail?: string;
};

/** Collapsed arguments / full result for one tool row. Renders nothing when the row has
 *  no sidecar — an old transcript row keeps its raw text and no toggle. */
function RowDetail({ row }: { row: Row }) {
  const [open, setOpen] = useState(false);
  const body = row.args || row.detail || "";
  if (!body) return null;
  return (
    <div className="mt-1">
      <button
        type="button"
        onClick={() => setOpen((s) => !s)}
        className="font-mono text-[11px] text-mast-muted hover:text-mast-text"
      >
        {open ? "▾" : "▸"} {row.args ? tr("参数", "Arguments") : tr("完整返回", "Full result")}
        {row.tool ? ` · ${row.tool}` : ""}
        {row.argsClipped ? tr(" · 已截断", " · truncated") : ""}
      </button>
      {open && (
        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded border border-mast-border/60 bg-mast-bg/60 p-2 font-mono text-[11px]">
          {body}
        </pre>
      )}
    </div>
  );
}

// content signature for dedup — role+text+second-bucket (the live SSE frame's t
// and the persisted _persist t differ by sub-second, so round to 1s).
function sig(role: string, text: string, t: number): string {
  return `${role}|${text}|${Math.round(t)}`;
}

function fmtTime(t?: number): string {
  if (!t) return "";
  try {
    return new Date(t * 1000).toLocaleString(langTag());
  } catch {
    return "";
  }
}

export function GroupActivityFeed({ agentId }: { agentId: string }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["agent-group-activity", agentId],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/agents/{agent_id}/group-activity", {
        params: { path: { agent_id: agentId } },
      });
      if (error) throw error;
      return data;
    },
    // Keep-alive polling stays: if the WS drops (tunnel hiccup, service restart)
    // the page must not freeze. With push it is only a fallback, so slow is fine.
    refetchInterval: 5000,
  });

  // A 5 s poll is too sluggish for watching an agent work.
  //
  // When a transcript row is persisted the backend already pushes a cursor-only
  // event (`chat/store.py` `_publish_transcript_append`) whose payload carries
  // `agent_id`, so this hooks in with no backend change. The event has no body;
  // the content is still fetched from the normal read endpoint.
  const seenTs = useRef(0);
  useWsEvent("experiment", (event) => {
    // "Is it this agent's message" plus the cursor check live in
    // lib/transcriptRefresh.ts: those rules are all about when NOT to refresh, and
    // missing one only shows as an occasional jump (no crash, not visible in a
    // screenshot, not caught by the type checker), so tests guard it.
    const { ok, t } = transcriptCursorFor(event.data as TranscriptEventData, agentId);
    if (!ok) return;
    // A burst of events (one group-chat turn persists several rows) collapses into
    // one re-read instead of one request per row. t === 0 means the event carried
    // no cursor, so refresh anyway rather than treating "no cursor" as "old cursor".
    if (t && t <= seenTs.current) return;
    if (t) seenTs.current = t;
    void qc.invalidateQueries({ queryKey: ["agent-group-activity", agentId] });
  });

  // LIVE slice — this agent's messages in the running group conversation, from
  // the run-task store. Subscribe to the STABLE store-owned `entries` reference
  // (it changes only when a frame arrives), then derive in a useMemo. A
  // `useShallow` selector that `.map`s into NEW object literals returns a fresh
  // array on every call → unstable getSnapshot → React #185 infinite render
  // (a crash in the agent conversation view). Never build objects inside a store selector.
  const entries = useRunTaskStore((s) => s.entries);
  const running = useRunTaskStore((s) => s.running);
  type Live = {
    role: string;
    text: string;
    t: number;
    tool?: string;
    args?: string;
    argsClipped?: boolean;
    detail?: string;
  };
  const live = useMemo(
    () =>
      entries
        .filter((e) => e.kind === "message" && e.agent === agentId)
        .map((e): Live | null =>
          e.kind === "message"
            ? {
                role: e.role as string,
                text: e.text,
                t: e.t,
                tool: e.tool,
                args: e.args,
                argsClipped: e.argsClipped,
                detail: e.detail,
              }
            : null,
        )
        .filter((x): x is Live => x != null),
    [entries, agentId],
  );

  const rows: Row[] = useMemo(() => {
    const seen = new Set<string>();
    const out: Row[] = [];
    // durable first (folds resume-replayed duplicates among themselves)
    for (const e of q.data?.entries ?? []) {
      const s = sig(e.role, e.text, e.t);
      if (seen.has(s)) continue;
      seen.add(s);
      out.push({
        key: `p-${e.conversation_id}-${e.seq}`,
        role: e.role,
        text: e.text,
        t: e.t,
        title: e.conversation_title || undefined,
        ...parseRowMeta(e.meta),
      });
    }
    // live rows not already persisted (real-time tool calls before the flush)
    live.forEach((l, i) => {
      const s = sig(l.role, l.text, l.t);
      if (seen.has(s)) return;
      seen.add(s);
      out.push({
        key: `l-${i}-${Math.round(l.t)}`,
        role: l.role,
        text: l.text,
        t: l.t,
        live: true,
        tool: l.tool,
        args: l.args,
        argsClipped: l.argsClipped,
        detail: l.detail,
      });
    });
    out.sort((a, b) => b.t - a.t); // newest first
    return out;
  }, [q.data, live]);

  const degraded = q.data?.degraded;

  return (
    <Card className="space-y-2 px-4 py-3">
      <div className="flex items-center gap-2">
        <Avatar id={agentId} size={18} />
        <h4 className="text-sm font-medium">
          {tr("群聊中的活动", "Group chat activity")}
          <span className="ml-2 text-xs font-normal text-mast-muted">
            {tr(`· ${agentLabel(agentId)} 在多智能体编排（群聊）中的发言`, `· Messages from ${agentLabel(agentId)} in multi-agent orchestration (group chat)`)}
          </span>
        </h4>
        {running && (
          <Badge tone="AUTO">
            <span className="mr-1 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-current align-middle" />
            {tr("实时", "Live")}
          </Badge>
        )}
        {rows.length > 0 && (
          <span className="ml-auto font-mono text-[10px] text-mast-muted">{tr(`${rows.length} 条`, `${rows.length} items`)}</span>
        )}
      </div>

      {q.isPending && rows.length === 0 && <Spinner />}
      {q.isError && rows.length === 0 && <ErrorNote error={q.error} />}
      {q.data && degraded && rows.length === 0 && (
        <DegradedNote what={tr("群聊活动（需可内省的会话存储）", "Group chat activity (requires introspectable session storage)")} />
      )}
      {!q.isPending && rows.length === 0 && !degraded && (
        <EmptyNote label={tr("该代理尚未在任何群聊中发言（或群聊未运行）。", "This agent has not spoken in any group chat (or group chat is not running).")} />
      )}

      {rows.length > 0 && (
        <ol className="max-h-[280px] space-y-2 overflow-auto">
          {rows.map((e) => (
            <li
              key={e.key}
              className={
                "rounded-md border bg-mast-bg/40 px-3 py-2 text-sm " +
                (e.live ? "border-mast-accent/40" : "border-mast-border")
              }
            >
              <div className="mb-1 flex items-center gap-2 text-xs text-mast-muted">
                {e.role === "tool" ? (
                  <span className="rounded bg-mast-bg px-1.5 py-0.5 font-mono">{tr("工具", "Tool")}</span>
                ) : (
                  <span className="rounded bg-mast-accent/10 px-1.5 py-0.5 text-mast-accent">{tr("发言", "Message")}</span>
                )}
                {e.live && <span className="text-mast-accent">{tr("· 实时", "· live")}</span>}
                {e.title && (
                  <span className="truncate" title={e.title}>
                    {tr(`群聊 · ${e.title}`, `Group chat · ${e.title}`)}
                  </span>
                )}
                <span className="ml-auto font-mono">{fmtTime(e.t)}</span>
              </div>
              <div
                className={
                  "whitespace-pre-wrap break-words " +
                  // Tool rows are sentences, not code fragments, so they are not
                  // rendered as monospace payload.
                  (e.role === "tool" ? "text-xs text-mast-muted" : "text-mast-text")
                }
              >
                {e.text}
              </div>
              <RowDetail row={e} />
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}
