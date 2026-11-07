import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { Card, Spinner, ErrorNote, DegradedNote, EmptyNote } from "../ui";
import { tr } from "../../lib/i18n";
import type { components } from "../../api/schema";

type SearchHit = components["schemas"]["SearchHit"];
type SearchResponse = components["schemas"]["SearchResponse"];

/** True when a failure message says the literature index is not installed
 *  (the backend raises "Literature index missing" when the index dir is absent). */
function isIndexMissingError(err: unknown): boolean {
  const msg = String((err as Error)?.message ?? err ?? "");
  return /index missing|no literature index|FileNotFoundError/i.test(msg);
}

/** Notice shown when no literature index is installed. */
function NoIndexNote() {
  return (
    <Card className="border-mast-warn-border">
      <p className="text-sm font-medium text-mast-warn">
        {tr("未安装文献索引", "No literature index installed")}
      </p>
      <p className="mt-1 text-xs text-mast-muted">
        {tr(
          "MAST 不再内置文献语料，大库语义检索需要你自己构建或提供一个文献索引才有内容可搜。你仍可在「库管理」创建文献库，并通过「摄取与取文」上传、摄取自己的论文。",
          "MAST no longer ships a built-in literature corpus, so semantic search over the big library has nothing to search until you build or provide a literature index. You can still create libraries under \"Libraries\" and upload or ingest your own papers under \"Ingest & fetch\".",
        )}
      </p>
    </Card>
  );
}

/** Semantic search over the big library, optionally scoped to a library's member set.
 *  Click a hit → load its full abstract (lazy GET /literature/abstract/{id}). */
export function SearchPanel({ libraryId }: { libraryId: string | null }) {
  const [query, setQuery] = useState("");
  const [material, setMaterial] = useState("");
  const [k, setK] = useState(8);
  const [openWorkId, setOpenWorkId] = useState<string | null>(null);

  const searchM = useMutation({
    mutationFn: async (): Promise<SearchResponse> => {
      const { data, error } = await api.POST("/api/literature/search", {
        body: {
          query: query.trim(),
          k,
          material: material.trim(),
          library_id: libraryId,
        },
      });
      if (error) throw error;
      return data;
    },
  });

  const res = searchM.data;
  const hits: SearchHit[] = res?.hits ?? [];

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-1 flex-col text-xs text-mast-muted">
            {tr("查询（语义）", "Query (semantic)")}
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && query.trim()) searchM.mutate();
              }}
              placeholder={tr("例如：CO tip functionalization on Cu(111)", "e.g. CO tip functionalization on Cu(111)")}
              className="mt-1 w-full rounded border border-mast-border bg-mast-bg px-2 py-1 text-sm text-mast-text"
            />
          </label>
          <label className="flex flex-col text-xs text-mast-muted">
            {tr("材料过滤", "Material filter")}
            <input
              value={material}
              onChange={(e) => setMaterial(e.target.value)}
              placeholder={tr("可选", "Optional")}
              className="mt-1 w-32 rounded border border-mast-border bg-mast-bg px-2 py-1 text-sm text-mast-text"
            />
          </label>
          <label className="flex flex-col text-xs text-mast-muted">
            {tr("返回数", "Results")}
            <input
              type="number"
              min={1}
              max={200}
              value={k}
              onChange={(e) => setK(Math.max(1, Math.min(200, Number(e.target.value) || 1)))}
              className="mt-1 w-20 rounded border border-mast-border bg-mast-bg px-2 py-1 text-sm text-mast-text"
            />
          </label>
          <button
            onClick={() => searchM.mutate()}
            disabled={!query.trim() || searchM.isPending}
            className="rounded bg-mast-accent/20 px-3 py-1.5 text-sm text-mast-accent hover:bg-mast-accent/30 disabled:opacity-40"
          >
            {searchM.isPending ? tr("检索中…", "Searching…") : tr("检索", "Search")}
          </button>
        </div>
        <p className="mt-2 text-xs text-mast-muted">
          {libraryId
            ? tr(`检索范围：库 ${libraryId}（大库检索 → 成员过滤）`, `Scope: library ${libraryId} (big-library search → member filter)`)
            : tr("检索范围：全大库", "Scope: the whole big library")}
        </p>
      </Card>

      {searchM.isPending && <Spinner label={tr("检索中…", "Searching…")} />}
      {searchM.isError && isIndexMissingError(searchM.error) && <NoIndexNote />}
      {searchM.isError && !isIndexMissingError(searchM.error) && <ErrorNote error={searchM.error} />}
      {res?.degraded && hits.length === 0 && <NoIndexNote />}
      {res?.degraded && hits.length > 0 && (
        <Card className="border-mast-warn-border">
          <p className="text-sm text-mast-warn">
            {tr(
              "语义检索暂不可用（缺 DashScope key / 网络不通 / 后端未接入），结果可能已降级为关键词匹配或为空。",
              "Semantic search is unavailable (missing DashScope key / network down / backend not wired); results may have fallen back to keyword matching or be empty.",
            )}
          </p>
        </Card>
      )}
      {res && !res.degraded && hits.length === 0 && searchM.isSuccess && (
        <EmptyNote label={tr("无匹配结果。", "No matching results.")} />
      )}

      {hits.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs text-mast-muted">{tr(`${res?.count ?? hits.length} 条结果`, `${res?.count ?? hits.length} result(s)`)}</p>
          {hits.map((h) => (
            <Card key={h.work_id || h.title}>
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="font-medium">{h.title || tr("(无标题)", "(untitled)")}</div>
                  <div className="mt-0.5 text-xs text-mast-muted">
                    {h.year || "?"}
                    {h.journal && ` · ${h.journal}`}
                    {tr(` · 被引 ${h.cited}`, ` · cited ${h.cited}`)}
                    {h.source && ` · ${h.source}`}
                    {` · ${h.retrieval}`}
                    {h.score > 0 && ` · score ${h.score.toFixed(3)}`}
                  </div>
                  <div className="mt-0.5 text-xs text-mast-muted">
                    <code>{h.work_id}</code>
                    {h.doi && (
                      <>
                        {" · "}
                        <a
                          href={`https://doi.org/${h.doi}`}
                          target="_blank"
                          rel="noreferrer"
                          className="text-mast-accent hover:underline"
                        >
                          {h.doi}
                        </a>
                      </>
                    )}
                  </div>
                  {h.abstract_excerpt && (
                    <p className="mt-2 text-sm text-mast-text/90">{h.abstract_excerpt}</p>
                  )}
                </div>
                <button
                  onClick={() =>
                    setOpenWorkId(openWorkId === h.work_id ? null : h.work_id)
                  }
                  disabled={!h.work_id}
                  className="shrink-0 rounded px-2 py-1 text-xs text-mast-accent hover:bg-mast-accent/20 disabled:opacity-30"
                >
                  {openWorkId === h.work_id ? tr("收起摘要", "Hide abstract") : tr("查看摘要", "View abstract")}
                </button>
              </div>
              {openWorkId === h.work_id && <AbstractView workId={h.work_id} />}
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

/** Full abstract for one work — lazy GET /literature/abstract/{work_id}. */
export function AbstractView({ workId }: { workId: string }) {
  const q = useQuery({
    queryKey: ["literature", "abstract", workId],
    queryFn: async () => {
      const { data, error } = await api.GET(
        "/api/literature/abstract/{work_id}",
        { params: { path: { work_id: workId } } },
      );
      if (error) throw error;
      return data;
    },
    enabled: !!workId,
  });

  const rec = q.data;

  return (
    <div className="mt-3 rounded border border-mast-border bg-mast-bg/50 p-3">
      {q.isPending && <Spinner label={tr("加载摘要…", "Loading abstract…")} />}
      {q.isError && <ErrorNote error={q.error} />}
      {rec?.degraded && <DegradedNote what={tr("摘要服务", "Abstract service")} />}
      {rec && !rec.degraded && !rec.found && (
        <EmptyNote label={`${tr("未找到摘要。", "Abstract not found.")}${rec.note || ""}`} />
      )}
      {rec && rec.found && (
        <div className="space-y-2">
          <div className="font-medium">{rec.title || workId}</div>
          <div className="text-xs text-mast-muted">
            {(rec.first_author || rec.authors) && (
              <>{rec.first_author || rec.authors} · </>
            )}
            {rec.year || "?"}
            {rec.journal && ` · ${rec.journal}`}
            {rec.source && ` · ${rec.source}`}
            {rec.cited_by_count > 0 && tr(` · 被引 ${rec.cited_by_count}`, ` · cited ${rec.cited_by_count}`)}
          </div>
          {rec.doi && (
            <div className="text-xs">
              <a
                href={`https://doi.org/${rec.doi}`}
                target="_blank"
                rel="noreferrer"
                className="text-mast-accent hover:underline"
              >
                {rec.doi}
              </a>
            </div>
          )}
          <pre className="whitespace-pre-wrap rounded bg-mast-panel p-2 text-sm text-mast-text/90">
            {rec.abstract || tr("(无摘要)", "(no abstract)")}
          </pre>
          {rec.user_abstract && (
            <div>
              <div className="text-xs text-mast-muted">{tr("用户摘要", "User abstract")} ({rec.abstract_provenance || "user"})</div>
              <pre className="whitespace-pre-wrap rounded bg-mast-panel p-2 text-sm text-mast-text/90">
                {rec.user_abstract}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
