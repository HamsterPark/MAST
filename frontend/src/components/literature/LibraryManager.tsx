import { Fragment, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { Card, Badge, Spinner, ErrorNote, DegradedNote, EmptyNote } from "../ui";
import type { components } from "../../api/schema";
import { useCurrentScope } from "../../api/scope";
import { libraryLabel } from "../../lib/libraryLabel";
import { tr } from "../../lib/i18n";

type LibrarySummary = components["schemas"]["LibrarySummary"];

const SCOPE_TONE: Record<string, string> = {
  global: "DANGEROUS",
  experiment: "WARN",
  custom: "INFO",
};

/** Library management — list / create / rename / delete pointer-set libraries.
 *  A library is a set of pointers into the big library: every paper lives in the
 *  big library and a library only holds work_id references.
 *  ``selectedId`` lets the parent (search panel) scope search to one library.
 *
 *  One dedicated library per experiment: the default target of ingest and lib_add
 *  is the *effective* library (``effective_library_id`` from the backend), which
 *  is the active experiment's own library when there is one. The UI therefore
 *  has to state three things:
 *
 *    - which library is the one that will actually be written to (not the one
 *      carrying the "active" badge)
 *    - "set as active" does not change the target while an experiment is active
 *      (it only sets the manual pointer used when there is no experiment)
 *    - libraries are not shared: to reuse another library's bibliography, copy it
 *      into the current experiment
 */
export function LibraryManager({
  selectedId,
  onSelect,
}: {
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}) {
  const qc = useQueryClient();
  const [newName, setNewName] = useState("");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const librariesQ = useQuery({
    queryKey: ["literature", "libraries"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/literature/libraries");
      if (error) throw error;
      return data;
    },
  });
  // Names an experiment library that has not been lazily created yet. Shares the
  // TopBar cache, so no extra request.
  const scope = useCurrentScope();

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["literature", "libraries"] });

  const createM = useMutation({
    mutationFn: async () => {
      // scope is fixed to custom: an experiment library's id is derived from the
      // experiment (exp_<id8>) and its members live in the experiment folder, so
      // only the backend can create it, lazily, on first use. Offering
      // "experiment" here would be silently downgraded to custom by the backend.
      const { data, error } = await api.POST("/api/literature/libraries", {
        body: { name: newName.trim(), scope: "custom" },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      setNotice(res.message || (res.ok ? tr("已创建", "Created") : tr("创建失败", "Create failed")));
      if (res.ok) setNewName("");
      invalidate();
    },
    onError: (e) => setNotice(tr(`创建失败：${String((e as Error)?.message ?? e)}`, `Create failed: ${String((e as Error)?.message ?? e)}`)),
  });

  const renameM = useMutation({
    mutationFn: async (vars: { id: string; name: string }) => {
      const { data, error } = await api.PATCH(
        "/api/literature/libraries/{library_id}",
        {
          params: { path: { library_id: vars.id } },
          body: { new_name: vars.name.trim() },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      setNotice(res.message || (res.ok ? tr("已重命名", "Renamed") : tr("重命名失败", "Rename failed")));
      setRenamingId(null);
      invalidate();
    },
    onError: (e) => setNotice(tr(`重命名失败：${String((e as Error)?.message ?? e)}`, `Rename failed: ${String((e as Error)?.message ?? e)}`)),
  });

  const deleteM = useMutation({
    mutationFn: async (id: string) => {
      const { data, error } = await api.DELETE(
        "/api/literature/libraries/{library_id}",
        { params: { path: { library_id: id } } },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (res, id) => {
      setNotice(res.message || (res.ok ? tr("已删除", "Deleted") : tr("未删除", "Not deleted")));
      if (res.ok && selectedId === id) onSelect(null);
      if (res.ok && expandedId === id) setExpandedId(null);
      invalidate();
    },
    onError: (e) => setNotice(tr(`删除失败：${String((e as Error)?.message ?? e)}`, `Delete failed: ${String((e as Error)?.message ?? e)}`)),
  });

  const activateM = useMutation({
    mutationFn: async (id: string) => {
      const { data, error } = await api.POST(
        "/api/literature/libraries/{library_id}/activate",
        { params: { path: { library_id: id } } },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      if (res.degraded) setNotice(tr("文献库后端不可用，未能设置手动指针。", "Literature backend unavailable; manual pointer not set."));
      else if (!res.ok) setNotice(res.message || tr("未能设置手动指针", "Could not set the manual pointer"));
      else if (effectiveSource === "experiment")
        // Make clear it has no effect yet: the target is still the experiment library.
        setNotice(
          tr(
            `已记下手动指针，但**当前不生效**：有活跃实验时落点仍是它的专属库 ` +
              `${effectiveId}。手动指针只在没有活跃实验时使用。`,
            `Manual pointer saved, but it is **not in effect**: while an experiment is active the target is still its own library ` +
              `${effectiveId}. The manual pointer is only used when no experiment is active.`,
          ),
        );
      else setNotice(res.message || tr("已设为默认库", "Set as default library"));
      invalidate();
    },
    onError: (e) =>
      setNotice(tr(`设置手动指针失败：${String((e as Error)?.message ?? e)}`, `Failed to set manual pointer: ${String((e as Error)?.message ?? e)}`)),
  });

  const copyM = useMutation({
    mutationFn: async (id: string) => {
      const { data, error } = await api.POST(
        "/api/literature/libraries/{library_id}/copy",
        {
          params: { path: { library_id: id } },
          body: { to_experiment_id: "" }, // empty = current experiment
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      if (res.degraded) setNotice(tr("文献库后端不可用，未能复制。", "Literature backend unavailable; nothing copied."));
      else setNotice(res.message || (res.ok ? tr("已复制", "Copied") : tr("未能复制", "Copy failed")));
      invalidate();
      qc.invalidateQueries({ queryKey: ["literature", "library-detail"] });
    },
    onError: (e) => setNotice(tr(`复制失败：${String((e as Error)?.message ?? e)}`, `Copy failed: ${String((e as Error)?.message ?? e)}`)),
  });

  const data = librariesQ.data;
  const libs: LibrarySummary[] = data?.libraries ?? [];
  const effectiveId = data?.effective_library_id ?? null;
  const effectiveSource = data?.effective_source ?? "";
  const experimentActive = effectiveSource === "experiment";
  const effectiveLabel = libraryLabel(
    effectiveId ?? "",
    effectiveSource,
    libs,
    scope.data?.experiment?.name,
  );

  return (
    <div className="space-y-4">
      {/* create row */}
      <Card>
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-col text-xs text-mast-muted">
            {tr("新建库名称", "New library name")}
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder={tr("例如：Ag(111) 自旋态", "e.g. Ag(111) spin states")}
              className="mt-1 w-56 rounded border border-mast-border bg-mast-bg px-2 py-1 text-sm text-mast-text"
            />
          </label>
          <button
            onClick={() => createM.mutate()}
            disabled={!newName.trim() || createM.isPending}
            className="rounded bg-mast-accent/20 px-3 py-1.5 text-sm text-mast-accent hover:bg-mast-accent/30 disabled:opacity-40"
          >
            {createM.isPending ? tr("创建中…", "Creating…") : tr("创建库", "Create library")}
          </button>
        </div>
        <p className="mt-2 text-xs text-mast-muted">
          {tr(
            "库 = 大库的指针集合。所有论文都在大库；库只持有 work_id 引用。",
            "A library is a set of pointers into the big library. All papers live in the big library; a library only holds work_id references.",
          )}
          <br />
          {tr(
            "每个实验自带一个专属库（首次收录文献时自动出现，不用在这里建）。这里建的是跨实验的自定义库，比如攒着的长期阅读清单。",
            "Each experiment has its own library (created automatically when the first paper is added; no need to create it here). Libraries created here are custom, cross-experiment ones, e.g. a long-term reading list.",
          )}
        </p>
        {/* Which library will actually be written to; the "active" badge no longer answers that. */}
        {data && !data.degraded && effectiveId && (
          <p className="mt-2 text-xs">
            {/* Name first, id after: an experiment library is created lazily and is
                not in libs before the first paper is added, so the id alone would be
                a bare exp_<id8>. */}
            <span className="text-mast-muted">{tr("当前生效的库：", "Effective library: ")}</span>
            <b className="text-mast-text">{effectiveLabel.text}</b>{" "}
            <code className="text-mast-accent">{effectiveId}</code>
            <span className="text-mast-muted">
              {experimentActive
                ? tr(" —— 当前实验的专属库；摄取和收录默认都落在这里。", " — the current experiment's own library; ingest and additions land here by default.")
                : effectiveSource === "manual"
                  ? tr(" —— 手动指定（当前没有活跃实验）。", " — set manually (no active experiment).")
                  : tr(" —— 默认兜底库。", " — the default fallback library.")}
              {effectiveLabel.pending && tr("（还没建出来，首次收录时自动建立）", " (not created yet; it is created when the first paper is added)")}
            </span>
          </p>
        )}
        {notice && <p className="mt-2 text-xs text-mast-accent">{notice}</p>}
      </Card>

      {/* list */}
      {librariesQ.isPending && <Spinner />}
      {librariesQ.isError && <ErrorNote error={librariesQ.error} />}
      {data?.degraded && <DegradedNote what={tr("文献库", "Literature libraries")} />}
      {data && !data.degraded && libs.length === 0 && (
        <EmptyNote label={tr("暂无文献库。", "No libraries yet.")} />
      )}

      {data && !data.degraded && libs.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-mast-border">
          <table className="w-full text-sm">
            <thead className="bg-mast-panel text-mast-muted">
              <tr>
                <th className="px-3 py-2 text-left">{tr("名称", "Name")}</th>
                <th className="px-3 py-2 text-left">{tr("范围", "Scope")}</th>
                <th className="px-3 py-2 text-right">{tr("成员", "Members")}</th>
                <th className="px-3 py-2 text-left">library_id</th>
                <th className="px-3 py-2 text-right">{tr("操作", "Actions")}</th>
              </tr>
            </thead>
            <tbody>
              {libs.map((l) => {
                const isGlobal = l.scope === "global";
                const isSelected = selectedId === l.library_id;
                const isExpanded = expandedId === l.library_id;
                const isEffective = l.library_id === effectiveId;
                return (
                  <Fragment key={l.library_id}>
                  <tr
                    className={
                      "border-t border-mast-border " +
                      (isSelected ? "bg-mast-accent/10" : "hover:bg-mast-bg/40")
                    }
                  >
                    <td className="px-3 py-2 align-top font-medium">
                      {renamingId === l.library_id ? (
                        <div className="flex items-center gap-1">
                          <input
                            value={renameValue}
                            onChange={(e) => setRenameValue(e.target.value)}
                            className="w-40 rounded border border-mast-border bg-mast-bg px-2 py-1 text-sm"
                          />
                          <button
                            onClick={() =>
                              renameM.mutate({ id: l.library_id, name: renameValue })
                            }
                            disabled={!renameValue.trim() || renameM.isPending}
                            className="rounded bg-mast-accent/20 px-2 py-1 text-xs text-mast-accent disabled:opacity-40"
                          >
                            {tr("保存", "Save")}
                          </button>
                          <button
                            onClick={() => setRenamingId(null)}
                            className="rounded px-2 py-1 text-xs text-mast-muted hover:text-mast-text"
                          >
                            {tr("取消", "Cancel")}
                          </button>
                        </div>
                      ) : (
                        <span className="flex flex-wrap items-center gap-2">
                          {isEffective && (
                            <Badge tone="AUTO">
                              {experimentActive ? tr("本实验", "This experiment") : tr("生效中", "In effect")}
                            </Badge>
                          )}
                          {l.is_active && !isEffective && (
                            <span title={tr("无活跃实验时才会用到的手动指针", "Manual pointer, used only when no experiment is active")}>
                              <Badge tone="INFO">{tr("手动指针", "Manual pointer")}</Badge>
                            </span>
                          )}
                          {l.name || tr("(未命名)", "(unnamed)")}
                          {isGlobal && <span title={tr("全局库不可删除", "The global library cannot be deleted")}>🔒</span>}
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 align-top">
                      <Badge tone={SCOPE_TONE[l.scope] ?? "INFO"}>{l.scope}</Badge>
                      {/* A bare exp_<id8> says nothing to the user; show the experiment name */}
                      {l.experiment_name && (
                        <div
                          className="mt-1 text-[11px] text-mast-muted"
                          title={l.experiment_id ?? ""}
                        >
                          {tr(`《${l.experiment_name}》`, `“${l.experiment_name}”`)}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2 text-right align-top tabular-nums">
                      {l.member_count}
                    </td>
                    <td className="px-3 py-2 align-top">
                      <code className="text-xs text-mast-muted">{l.library_id}</code>
                    </td>
                    <td className="px-3 py-2 text-right align-top">
                      <div className="flex justify-end gap-1">
                        <button
                          onClick={() => onSelect(isSelected ? null : l.library_id)}
                          className="rounded px-2 py-1 text-xs text-mast-accent hover:bg-mast-accent/20"
                        >
                          {isSelected ? tr("取消检索范围", "Clear search scope") : tr("检索此库", "Search this library")}
                        </button>
                        <button
                          onClick={() =>
                            setExpandedId(
                              expandedId === l.library_id ? null : l.library_id,
                            )
                          }
                          className="rounded px-2 py-1 text-xs text-mast-muted hover:text-mast-text"
                        >
                          {expandedId === l.library_id ? tr("收起成员", "Hide members") : tr("成员", "Members")}
                        </button>
                        {/* Libraries are not shared; reuse another bibliography by copying it */}
                        {experimentActive && !isEffective && (
                          <button
                            onClick={() => copyM.mutate(l.library_id)}
                            disabled={copyM.isPending || l.member_count === 0}
                            title={
                              l.member_count === 0
                                ? tr("这个库还没有成员，没什么可复制", "This library has no members yet; nothing to copy")
                                : tr("把这个库的文献复制进当前实验的专属库（原库不动）", "Copy this library's papers into the current experiment's own library (the source is left unchanged)")
                            }
                            className="rounded px-2 py-1 text-xs text-mast-accent hover:bg-mast-accent/20 disabled:opacity-30"
                          >
                            {tr("复制到本实验", "Copy to this experiment")}
                          </button>
                        )}
                        <button
                          onClick={() => activateM.mutate(l.library_id)}
                          disabled={l.is_active || activateM.isPending}
                          title={
                            l.is_active
                              ? tr("已是手动指针", "Already the manual pointer")
                              : experimentActive
                                ? tr("设为手动指针 —— 有活跃实验时不改变落点，只在没有活跃实验时使用", "Set as manual pointer — does not change the target while an experiment is active; only used when none is")
                                : tr("设为默认库", "Set as default library")
                          }
                          className="rounded px-2 py-1 text-xs text-mast-accent hover:bg-mast-accent/20 disabled:opacity-40"
                        >
                          {l.is_active
                            ? tr("已指定", "Assigned")
                            : experimentActive
                              ? tr("设为手动指针", "Set manual pointer")
                              : tr("设为默认", "Set default")}
                        </button>
                        <button
                          onClick={() => {
                            setRenamingId(l.library_id);
                            setRenameValue(l.name);
                          }}
                          className="rounded px-2 py-1 text-xs text-mast-muted hover:text-mast-text"
                        >
                          {tr("重命名", "Rename")}
                        </button>
                        <button
                          onClick={() => deleteM.mutate(l.library_id)}
                          disabled={isGlobal || deleteM.isPending}
                          title={isGlobal ? tr("全局库不可删除", "The global library cannot be deleted") : tr("删除库", "Delete library")}
                          className="rounded px-2 py-1 text-xs text-mast-danger hover:bg-mast-danger-bg disabled:opacity-30"
                        >
                          {tr("删除", "Delete")}
                        </button>
                      </div>
                    </td>
                  </tr>
                  {isExpanded && (
                    <tr className="border-t border-mast-border bg-mast-bg/30">
                      <td colSpan={5} className="px-3 py-2">
                        <MemberList libraryId={l.library_id} />
                      </td>
                    </tr>
                  )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** Per-library member pointer list — GET /api/literature/libraries/{id} (detail
 *  returns members), with per-row remove via POST .../members/remove. */
function MemberList({ libraryId }: { libraryId: string }) {
  const qc = useQueryClient();
  const [notice, setNotice] = useState<string | null>(null);

  const detailQ = useQuery({
    queryKey: ["literature", "library-detail", libraryId],
    queryFn: async () => {
      const { data, error } = await api.GET(
        "/api/literature/libraries/{library_id}",
        { params: { path: { library_id: libraryId } } },
      );
      if (error) throw error;
      return data;
    },
  });

  const removeM = useMutation({
    mutationFn: async (workId: string) => {
      const { data, error } = await api.POST(
        "/api/literature/libraries/{library_id}/members/remove",
        {
          params: { path: { library_id: libraryId } },
          body: { work_ids: [workId] },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (res) => {
      if (res.degraded) setNotice(tr("文献库后端不可用，未能移除指针。", "Literature backend unavailable; pointer not removed."));
      else setNotice(res.message || (res.ok ? tr("已移除", "Removed") : tr("未移除", "Not removed")));
      qc.invalidateQueries({
        queryKey: ["literature", "library-detail", libraryId],
      });
      qc.invalidateQueries({ queryKey: ["literature", "libraries"] });
    },
    onError: (e) => setNotice(tr(`移除失败：${String((e as Error)?.message ?? e)}`, `Remove failed: ${String((e as Error)?.message ?? e)}`)),
  });

  const detail = detailQ.data;
  const members = detail?.library?.members ?? [];

  return (
    <div className="space-y-2">
      <div className="text-xs font-medium text-mast-muted">{tr("成员指针", "Member pointers")}</div>
      {detailQ.isPending && <Spinner />}
      {detailQ.isError && <ErrorNote error={detailQ.error} />}
      {detail?.degraded && <DegradedNote what={tr("成员列表", "Member list")} />}
      {detail && !detail.degraded && !detail.found && (
        <EmptyNote label={tr("未找到该库。", "Library not found.")} />
      )}
      {detail && !detail.degraded && detail.found && members.length === 0 && (
        <EmptyNote label={tr("此库暂无成员指针。", "This library has no member pointers yet.")} />
      )}
      {detail && !detail.degraded && members.length > 0 && (
        <div className="divide-y divide-mast-border overflow-hidden rounded border border-mast-border">
          {members.map((mem) => (
            <div
              key={mem.work_id}
              className="flex flex-wrap items-center gap-2 px-2 py-1.5 text-xs"
            >
              <code className="text-mast-accent">{mem.work_id}</code>
              {mem.doi && <span className="text-mast-muted">DOI {mem.doi}</span>}
              {mem.source && <Badge tone="INFO">{mem.source}</Badge>}
              {/* Whether the full text is on this machine; needed to decide whether to ask for an upload */}
              {mem.fulltext_status === "ingested" ? (
                <span title={mem.fulltext_ref ?? ""}>
                  <Badge tone="AUTO">{tr("全文在本机", "Full text local")}</Badge>
                </span>
              ) : mem.fulltext_status === "requested" ? (
                <Badge tone="WARN">{tr("已请求全文", "Full text requested")}</Badge>
              ) : null}
              {mem.copied_from && (
                <span className="text-mast-muted" title={tr("来自另一个库的复制", "Copied from another library")}>
                  {tr("复制自", "Copied from")} {mem.copied_from}
                </span>
              )}
              {mem.reason && (
                <span className="text-mast-muted">{tr("理由：", "Reason: ")}{mem.reason}</span>
              )}
              <button
                onClick={() => removeM.mutate(mem.work_id)}
                disabled={removeM.isPending}
                title={tr("从此库移除该指针（论文仍留在大库）", "Remove this pointer from the library (the paper stays in the big library)")}
                className="ml-auto rounded px-2 py-0.5 text-xs text-mast-danger hover:bg-mast-danger-bg disabled:opacity-30"
              >
                {tr("移除指针", "Remove pointer")}
              </button>
            </div>
          ))}
        </div>
      )}
      {notice && <p className="text-xs text-mast-accent">{notice}</p>}
    </div>
  );
}
