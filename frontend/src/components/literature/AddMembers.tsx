import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { Card, Badge, DegradedNote } from "@/components/ui";
import { Field, TextField, Button } from "@/components/controls";
import { tr } from "@/lib/i18n";

// Add/remove library members — POST /api/literature/libraries/{id}/members (add) and
// POST /api/literature/libraries/{id}/members/remove (remove). Paste work_ids /
// DOIs (comma or newline separated); each is a POINTER into the chosen library
// (the paper itself lives in the big library). The per-library member list is
// rendered in LibraryManager (expand "Members").

function parseIds(text: string): string[] {
  return text
    .replace(/,/g, "\n")
    .split("\n")
    .map((t) => t.trim())
    .filter(Boolean);
}

export function AddMembers({ libraryId }: { libraryId: string | null }) {
  const qc = useQueryClient();
  const [idsText, setIdsText] = useState("");
  const [reason, setReason] = useState("");

  const m = useMutation({
    mutationFn: async () => {
      if (!libraryId) throw new Error(tr("先在上方选择一个库（检索此库）", "Select a library above first (\"Search this library\")"));
      const ids = parseIds(idsText);
      if (ids.length === 0) throw new Error(tr("粘贴至少一个 work_id / DOI", "Paste at least one work_id / DOI"));
      const { data, error } = await api.POST(
        "/api/literature/libraries/{library_id}/members",
        {
          params: { path: { library_id: libraryId } },
          body: { work_ids: ids, reason: reason.trim(), added_by: "user" },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      if (d?.ok) setIdsText("");
      qc.invalidateQueries({ queryKey: ["literature", "libraries"] });
    },
  });

  const removeM = useMutation({
    mutationFn: async () => {
      if (!libraryId) throw new Error(tr("先在上方选择一个库（检索此库）", "Select a library above first (\"Search this library\")"));
      const ids = parseIds(idsText);
      if (ids.length === 0) throw new Error(tr("粘贴至少一个 work_id / DOI", "Paste at least one work_id / DOI"));
      const { data, error } = await api.POST(
        "/api/literature/libraries/{library_id}/members/remove",
        {
          params: { path: { library_id: libraryId } },
          body: { work_ids: ids },
        },
      );
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      if (d?.ok) setIdsText("");
      qc.invalidateQueries({ queryKey: ["literature", "libraries"] });
      if (libraryId)
        qc.invalidateQueries({
          queryKey: ["literature", "library-detail", libraryId],
        });
    },
  });

  const res = m.data;
  const rmRes = removeM.data;
  return (
    <Card>
      <h3 className="mb-2 text-sm font-semibold">{tr("加入库成员（指针）", "Add library members (pointers)")}</h3>
      <p className="mb-3 text-xs text-mast-muted">
        {libraryId ? (
          <>
            {tr("目标库", "Target library")} <code>{libraryId}</code>
            {tr("。粘贴 work_id / DOI（逗号或换行分隔），每个加入为指针。", ". Paste work_ids / DOIs (comma- or newline-separated); each is added as a pointer.")}
          </>
        ) : (
          tr("先在上方库列表中点「检索此库」选定目标库，再加入成员。", "Pick a target library with \"Search this library\" in the list above, then add members.")
        )}
      </p>
      <div className="space-y-3">
        <Field label="work_ids / DOIs">
          <textarea
            value={idsText}
            onChange={(e) => setIdsText(e.target.value)}
            rows={3}
            placeholder="W2912345678, 10.1103/PhysRevLett.…"
            className="rounded-md border border-mast-border bg-mast-bg px-2.5 py-1.5 font-mono text-sm text-mast-text outline-none focus:border-mast-accent"
          />
        </Field>
        <Field label={tr("理由（可选）", "Reason (optional)")}>
          <TextField value={reason} onChange={setReason} placeholder={tr("为何加入此库", "Why this paper belongs in the library")} />
        </Field>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            disabled={!libraryId || !idsText.trim() || m.isPending}
            onClick={() => m.mutate()}
          >
            {m.isPending ? tr("加入中…", "Adding…") : tr("加入指针", "Add pointers")}
          </Button>
          <Button
            variant="ghost"
            disabled={!libraryId || !idsText.trim() || removeM.isPending}
            onClick={() => removeM.mutate()}
          >
            {removeM.isPending ? tr("移除中…", "Removing…") : tr("移除指针", "Remove pointers")}
          </Button>
        </div>
        <p className="text-xs text-mast-muted">
          {tr(
            "「移除指针」仅从此库删除上面列出的 work_id 指针；论文本身仍留在大库。完整成员列表见上方库列表的「成员」展开。",
            "\"Remove pointers\" only deletes the listed work_id pointers from this library; the papers themselves stay in the big library. Expand \"Members\" in the library list above for the full member list.",
          )}
        </p>
      </div>
      {m.isError && (
        <p className="mt-3 text-sm text-mast-danger">
          {tr("加入失败：", "Add failed: ")}{String((m.error as Error)?.message ?? m.error)}
        </p>
      )}
      {removeM.isError && (
        <p className="mt-3 text-sm text-mast-danger">
          {tr("移除失败：", "Remove failed: ")}{String((removeM.error as Error)?.message ?? removeM.error)}
        </p>
      )}
      {res && (
        <div className="mt-3 space-y-1 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={res.ok ? "AUTO" : "DANGEROUS"}>{res.message}</Badge>
            {res.added && res.added.length > 0 && (
              <Badge tone="INFO">{tr("加入", "Added")} {res.added.length}</Badge>
            )}
            {res.skipped && res.skipped.length > 0 && (
              <Badge tone="WARN">{tr("跳过", "Skipped")} {res.skipped.length}</Badge>
            )}
            {res.rejected && res.rejected.length > 0 && (
              <Badge tone="DANGEROUS">{tr("拒绝", "Rejected")} {res.rejected.length}</Badge>
            )}
            {res.at_cap && <Badge tone="WARN">{tr("已达上限", "At cap")}</Badge>}
          </div>
          {res.degraded && <DegradedNote what={tr("文献库后端", "Literature backend")} />}
          <p className="text-mast-muted tabular-nums">
            {tr("成员数：", "Members: ")}{res.member_count}
          </p>
        </div>
      )}
      {rmRes && (
        <div className="mt-3 space-y-1 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={rmRes.ok ? "AUTO" : "DANGEROUS"}>
              {rmRes.message || (rmRes.ok ? tr("已移除", "Removed") : tr("未移除", "Not removed"))}
            </Badge>
            {rmRes.n_removed > 0 && (
              <Badge tone="INFO">{tr("移除", "Removed")} {rmRes.n_removed}</Badge>
            )}
          </div>
          {rmRes.degraded && <DegradedNote what={tr("文献库后端", "Literature backend")} />}
          <p className="text-mast-muted tabular-nums">
            {tr("成员数：", "Members: ")}{rmRes.member_count}
          </p>
        </div>
      )}
    </Card>
  );
}
