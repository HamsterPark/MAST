import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import { useCurrentScope } from "@/api/scope";
import { libraryLabel } from "@/lib/libraryLabel";
import { Section } from "@/components/ui";
import { SubTabs } from "@/components/controls";
import { LibraryManager } from "@/components/literature/LibraryManager";
import { SearchPanel, AbstractView } from "@/components/literature/SearchPanel";
import { AddMembers } from "@/components/literature/AddMembers";
import {
  IngestReadiness,
  IngestPanel,
  FetchPanel,
} from "@/components/literature/IngestFetch";
import { FetchBoard } from "@/components/literature/FetchBoard";
import { PdfUploadPanel, ManualEntryPanel } from "@/components/literature/UploadManual";
import { useStickyTab } from "@/hooks/useStickyTab";
import { IncompleteBadge } from "@/components/shell/LegacyOnly";
import { tr } from "@/lib/i18n";

// Literature page, over the typed FastAPI routes. Flat SubTabs (no nested tab
// groups, so the UI cannot freeze):
//   1. Libraries      — list / create / rename / delete pointer-set libraries + add members
//   2. Semantic search — search the big library (optionally scoped to a library)
//   3. Abstract lookup — fetch a full abstract directly by work_id
//   4. Ingest & fetch  — PDF ingest + DOI/URL fetch + readiness probe
//   5. Request board   — agent-asks-user full-text request board
// The big library is the real library; every other library is a set of pointers
// into it. All papers go into the big library.

type View = "libraries" | "search" | "abstract" | "ingest" | "board";

const VIEW_TABS: { id: View; label: string }[] = [
  { id: "libraries", label: tr("库管理", "Libraries") },
  { id: "search", label: tr("语义检索", "Semantic search") },
  { id: "abstract", label: tr("摘要查询", "Abstract lookup") },
  { id: "ingest", label: tr("摄取与取文", "Ingest & fetch") },
  { id: "board", label: tr("取文请求板", "Request board") },
];

export default function LiteraturePage() {
  // Remember the last sub-tab: the request board can hold pending requests (the
  // top-bar badge counts them), and being sent back to the first tab on every
  // return would force the user to click through to the thing the badge points at.
  const [view, setView] = useStickyTab<View>(
    "literature", VIEW_TABS.map((t) => t.id), "libraries");
  // The selected library id scopes semantic search / ingest / adding members to
  // that library.
  //
  // null = nothing picked yet → use the backend's *effective* library (the
  // current experiment's own library; the manual pointer when there is no active
  // experiment, then the "reading" fallback). Without this the backend's active
  // library and the frontend's selected library would be two unrelated concepts,
  // and AddMembers could only say "select a library above first".
  const [pickedLibraryId, setPickedLibraryId] = useState<string | null>(null);
  const [abstractInput, setAbstractInput] = useState("");
  const [abstractWorkId, setAbstractWorkId] = useState<string | null>(null);
  // A fetched PDF path can flow into the ingest panel.
  const [fetchedPath, setFetchedPath] = useState<string | null>(null);
  // Current experiment, used to name an experiment library that does not exist yet (see libraryLabel below).
  const scope = useCurrentScope();

  // Pending full-text-request banner: surfaces (polled every 5 s) at the top of the
  // page regardless of which sub-view is open, so the user sees agent requests
  // without opening the request board. Read-only, cheap, safe to poll.
  const boardQ = useQuery({
    queryKey: ["literature", "fetch-board", ""],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/literature/fetch-board", {
        params: { query: { status: null } },
      });
      if (error) throw error;
      return data;
    },
    refetchInterval: 5000,
  });
  const pending = boardQ.data?.pending_count ?? 0;

  // Effective library: one dedicated library per experiment, following experiment
  // switches (the backend recomputes it from active_scope on every read).
  const libsQ = useQuery({
    queryKey: ["literature", "libraries", "effective"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/literature/libraries");
      if (error) throw error;
      return data;
    },
    refetchInterval: 30_000,
  });
  const effectiveId = libsQ.data?.effective_library_id ?? null;
  const effectiveSource = libsQ.data?.effective_source ?? "";
  const selectedLibraryId = pickedLibraryId ?? effectiveId;
  const usingEffective = pickedLibraryId === null && !!effectiveId;
  // Show a name, not an id. Experiment libraries are created lazily, so before the
  // experiment's first paper is added effective_library_id points to a library
  // that is not in the registry yet and `find()` misses; the experiment name is
  // used instead of a bare exp_<id8>. The experiment name uses the queryKey the
  // TopBar already polls, so no extra request.
  const label = libraryLabel(
    effectiveId ?? "",
    effectiveSource,
    libsQ.data?.libraries ?? [],
    scope.data?.experiment?.name,
  );

  return (
    <div>
      <p className="mb-3 text-sm text-mast-muted">
        <strong className="text-mast-text">{tr("文献库", "Literature")}</strong>
        <IncompleteBadge /> —{" "}
        {tr(
          "大库是唯一真实的库；其他库都是大库的指针(work_id)。用户添加的论文都促进进大库，库只持有引用。",
          "The big library is the only real library; every other library is a set of pointers (work_ids) into it. Papers you add are promoted into the big library, and a library only holds references.",
        )}
      </p>

      <div className="mb-3 rounded-lg border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-sm text-mast-warn">
        {tr(
          "文献库功能尚不完善，仍在开发中。MAST 不再内置文献语料：大库的语义检索与摘要查询在没有文献索引时没有内容可查，除非你自己构建或提供一个索引。你仍然可以创建文献库，并通过上传 / 摄取添加自己的论文。",
          "The literature library is incomplete and still under development. MAST no longer ships a built-in literature corpus: semantic search and abstract lookup over the big library have nothing to search unless you build or provide an index yourself. You can still create libraries and add your own papers (upload / ingest).",
        )}
      </div>

      {pending > 0 && (
        <button
          onClick={() => setView("board")}
          className="mb-3 block w-full rounded-lg border border-mast-warn-border bg-mast-warn-bg px-3 py-2 text-left text-sm text-mast-warn hover:bg-mast-warn-border"
        >
          📥{" "}
          {tr(
            `文献 agent 有 ${pending} 条全文请求待你应答 —— 见「取文请求板」，上传 PDF / 取文即可满足。`,
            `The literature agent has ${pending} pending full-text request(s) for you — see "Request board"; upload the PDF or fetch it to fulfil them.`,
          )}
        </button>
      )}

      {/* Effective library: the user should see in every sub-tab where papers will land. */}
      {effectiveId && (
        <p className="mb-3 text-xs text-mast-muted">
          {tr("当前生效的库：", "Effective library: ")}
          <b className="text-mast-text" title={effectiveId}>
            {label.text}
          </b>
          {effectiveSource === "experiment"
            ? tr("（当前实验的专属库）", " (the current experiment's own library)")
            : effectiveSource === "manual"
              ? tr("（手动指定；当前没有活跃实验）", " (set manually; no active experiment)")
              : tr("（默认库）", " (default library)")}
          {/* The name is derived from the experiment and the library is not on disk yet; do not make it look as if it already exists. */}
          {label.pending && tr("，首次收录文献时自动建立", ", created automatically when the first paper is added")}
          {!usingEffective && (
            <>
              {tr(" · 你已手动选择了别的库 ", " · you picked another library ")}
              <button
                className="underline decoration-dotted"
                onClick={() => setPickedLibraryId(null)}
              >
                {tr("恢复为生效库", "Restore effective library")}
              </button>
            </>
          )}
        </p>
      )}

      <SubTabs<View> value={view} onChange={setView} tabs={VIEW_TABS} />

      {view === "libraries" && (
        <Section title={tr("文献库管理", "Library management")}>
          <div className="space-y-4">
            <LibraryManager
              selectedId={selectedLibraryId}
              onSelect={(id) => setPickedLibraryId(id)}
            />
            <AddMembers libraryId={selectedLibraryId} />
            {/* Direct entry management: manually add an entry that is not in OpenAlex and has no PDF. */}
            <ManualEntryPanel libraryId={selectedLibraryId} />
          </div>
        </Section>
      )}

      {view === "search" && (
        <Section title={tr("语义检索（大库）", "Semantic search (big library)")}>
          <SearchPanel libraryId={selectedLibraryId} />
        </Section>
      )}

      {view === "abstract" && (
        <Section title={tr("摘要查询（按 work_id）", "Abstract lookup (by work_id)")}>
          <div className="space-y-4">
            <div className="flex items-end gap-2">
              <label className="flex flex-1 flex-col text-xs text-mast-muted">
                work_id
                <input
                  value={abstractInput}
                  onChange={(e) => setAbstractInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && abstractInput.trim())
                      setAbstractWorkId(abstractInput.trim());
                  }}
                  placeholder={tr("例如：W2912345678", "e.g. W2912345678")}
                  className="mt-1 w-full rounded border border-mast-border bg-mast-bg px-2 py-1 text-sm text-mast-text"
                />
              </label>
              <button
                onClick={() => setAbstractWorkId(abstractInput.trim() || null)}
                disabled={!abstractInput.trim()}
                className="rounded bg-mast-accent/20 px-3 py-1.5 text-sm text-mast-accent hover:bg-mast-accent/30 disabled:opacity-40"
              >
                {tr("查询摘要", "Look up abstract")}
              </button>
            </div>
            {abstractWorkId && <AbstractView workId={abstractWorkId} />}
          </div>
        </Section>
      )}

      {view === "ingest" && (
        <Section title={tr("摄取与取文", "Ingest & fetch")}>
          <div className="space-y-4">
            <IngestReadiness />
            {/* Attach a PDF: upload straight from the browser (primary path). */}
            <PdfUploadPanel libraryId={selectedLibraryId} />
            <FetchPanel onFetchedPath={(p) => setFetchedPath(p)} />
            <IngestPanel
              key={fetchedPath ?? "ingest"}
              libraryId={selectedLibraryId}
            />
            {fetchedPath && (
              <p className="text-xs text-mast-muted">
                {tr("上次取到的 PDF：", "Last fetched PDF: ")}<code>{fetchedPath}</code>{tr("（可复制到上方摄取路径）", " (copy it into the ingest path above)")}
              </p>
            )}
          </div>
        </Section>
      )}

      {view === "board" && (
        <Section title={tr("取文请求板（agent 请你取全文）", "Request board (the agent asks you for full text)")}>
          <FetchBoard />
        </Section>
      )}
    </div>
  );
}
