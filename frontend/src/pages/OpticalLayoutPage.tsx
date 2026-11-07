import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useBlocker, useSearchParams } from "react-router-dom";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import { EmptyNote, ErrorNote, Section, Spinner } from "@/components/ui";
import { LayoutEditor } from "@/components/opticallayout/LayoutEditor";
import { NewLayoutDialog } from "@/components/opticallayout/LayoutDialogs";
import { useOperatorName } from "@/hooks/useOperatorPrefs";
import { isUnsupported } from "@/api/opticalLayout";
import { useArchiveLayout, useLayoutDocument, useLayoutList } from "@/api/opticalLayoutHooks";
import { emptyLayout } from "@/lib/opticalLayout/edit";
import { layoutOptionLabel, pickDefaultLayout } from "@/lib/opticalLayout/listing";
import { classifySaveError, saveFailureText, withOperator } from "@/lib/opticalLayout/saveFlow";
import type { OpticalLayout, TableSpec } from "@/lib/opticalLayout/types";

// Optical layout (docs/v2/design/optical_layout.md): the operator draws the optical path of
// the setup, places every component to 0.01 mm / 0.01 degree, annotates it (model, maker,
// function, motorized, connection, ...) and talks about it with the AI. The page picks a
// layout and hands it to <LayoutEditor>; the editor owns everything about the drawing.
//
// It is reachable on both station profiles: an STM station with an optical path (TERS) and
// the optical station both have one to describe.

const CONFIRM_LEAVE = () => tr("有未保存的修改，离开会丢掉它们。继续吗？", "You have unsaved changes; leaving discards them. Continue?");

export default function OpticalLayoutPage() {
  const [params, setParams] = useSearchParams();
  const list = useLayoutList();
  const archive = useArchiveLayout();
  const [operator] = useOperatorName();
  /** a layout that exists only in this tab until its first save */
  const [draft, setDraft] = useState<OpticalLayout | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [archiveMsg, setArchiveMsg] = useState<string | null>(null);
  const dirty = useRef(false);
  const setDirty = useCallback((d: boolean) => {
    dirty.current = d;
  }, []);

  const layouts = useMemo(() => list.data ?? [], [list.data]);
  const taken = useMemo(() => new Set(layouts.map((l) => l.id)), [layouts]);
  const wanted = params.get("layout");
  const id = wanted ?? draft?.id ?? pickDefaultLayout(layouts);
  const isDraft = draft !== null && draft.id === id;
  const doc = useLayoutDocument(isDraft ? null : id);
  const unsupported = list.isError && isUnsupported(list.error);
  const summary = layouts.find((l) => l.id === id);

  // A link without ?layout= gets one, so what is on screen can be bookmarked and shared.
  useEffect(() => {
    if (!wanted && id) setParams({ layout: id }, { replace: true });
  }, [wanted, id, setParams]);

  // In-app navigation away from unsaved edits asks first (a page reload is covered in the editor).
  const blocker = useBlocker(({ currentLocation, nextLocation }) => dirty.current && currentLocation.pathname !== nextLocation.pathname);
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    if (window.confirm(CONFIRM_LEAVE())) blocker.proceed();
    else blocker.reset();
  }, [blocker]);

  const open = useCallback(
    (next: string, force = false) => {
      if (!force && dirty.current && !window.confirm(CONFIRM_LEAVE())) return;
      dirty.current = false;
      setDraft(null);
      setArchiveMsg(null);
      setParams({ layout: next });
    },
    [setParams],
  );

  const create = (newId: string, title: string, table: TableSpec) => {
    if (dirty.current && !window.confirm(CONFIRM_LEAVE())) return;
    dirty.current = false;
    setDraft(emptyLayout(newId, title, table));
    setNewOpen(false);
    setArchiveMsg(null);
    setParams({ layout: newId });
  };

  const doArchive = () => {
    if (!summary) return;
    const body = withOperator(operator, { base_revision: summary.revision });
    if (!body.ok) return setArchiveMsg(body.error);
    const ok = window.confirm(
      tr(
        `把“${summary.title || summary.id}”归档？它会移到归档目录，不会真的删除，之后仍可在服务器的 archive/ 里找到。`,
        `Archive “${summary.title || summary.id}”? It moves to the archive folder and is not really deleted; you can still find it under archive/ on the server.`,
      ),
    );
    if (!ok) return;
    archive.mutate(
      { id: summary.id, body: body.value },
      {
        // leave the archived layout only once the list no longer has it, or the default pick would be it again
        onSuccess: () => {
          dirty.current = false;
          setArchiveMsg(null);
          void list.refetch().then(() => setParams({}, { replace: true }));
        },
        onError: (e) => setArchiveMsg(saveFailureText(classifySaveError(e))),
      },
    );
  };

  const header = (
    <div className="flex flex-wrap items-center justify-end gap-2">
      {layouts.length > 0 && (
        <label className="flex items-center gap-1.5 text-sm text-mast-muted">
          <span className="sr-only">{tr("选择光路图", "Choose a layout")}</span>
          <select
            value={id && layouts.some((l) => l.id === id) ? id : ""}
            onChange={(e) => e.target.value && open(e.target.value)}
            className="max-w-[18rem] rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-2 py-1.5 text-sm text-mast-text"
            aria-label={tr("选择光路图", "Choose a layout")}
          >
            {isDraft && !layouts.some((l) => l.id === id) && <option value="">{`${draft?.title || draft?.id} (${tr("未保存", "unsaved")})`}</option>}
            {layouts.map((l) => (
              <option key={l.id} value={l.id} disabled={Boolean(l.error)} title={l.error}>
                {layoutOptionLabel(l)}
              </option>
            ))}
          </select>
        </label>
      )}
      <button type="button" className="rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-3 py-1.5 text-sm text-mast-text hover:bg-mast-panel-2" onClick={() => setNewOpen(true)} disabled={unsupported}>
        + {tr("新建", "New")}
      </button>
      <button
        type="button"
        disabled={!summary || archive.isPending}
        title={tr("归档这张光路图（移到 archive/，不真删）", "Archive this layout (moved to archive/, not deleted)")}
        className={clsx("rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-3 py-1.5 text-sm text-mast-text hover:bg-mast-panel-2 disabled:opacity-50")}
        onClick={doArchive}
      >
        {tr("归档", "Archive")}
      </button>
    </div>
  );

  let body;
  if (unsupported) {
    body = <EmptyNote label={tr("这个 MAST 服务还没有光路图接口（/api/optical-layouts）。升级到带光路图的版本后这里才能用。", "This MAST service has no optical layout API (/api/optical-layouts) yet. The page works once it is upgraded to a build that has it.")} />;
  } else if (list.isPending && !isDraft) {
    body = <Spinner />;
  } else if (list.isError && !isDraft && layouts.length === 0) {
    body = <ErrorNote error={list.error} label={tr("光路图列表读取失败", "Could not read the list of layouts")} />;
  } else if (id === null) {
    body = (
      <div className="flex flex-col items-start gap-3 rounded-mast-card border border-dashed border-mast-border-strong p-6">
        <p className="text-sm text-mast-text">{tr("还没有光路图。画一张：放上激光器、镜子、物镜，标上型号和接线，AI 就能照着它和你讨论光路。", "No layouts yet. Draw one: place the laser, mirrors and objective, note their models and wiring, and the AI can discuss the optical path with you from it.")}</p>
        <button type="button" className="rounded-mast-ctl bg-mast-accent px-4 py-2 text-sm font-semibold text-mast-accent-ink hover:opacity-90" onClick={() => setNewOpen(true)}>
          {tr("新建第一张光路图", "Create the first layout")}
        </button>
      </div>
    );
  } else if (isDraft && draft) {
    body = <LayoutEditor key={draft.id} initial={draft} linksState={{}} taken={taken} onDirtyChange={setDirty} onSwitchTo={(n) => open(n, true)} />;
  } else if (doc.isPending) {
    body = <Spinner label={tr("正在载入光路图…", "Loading the layout…")} />;
  } else if (doc.isError) {
    body = (
      <div className="space-y-2">
        <ErrorNote error={doc.error} label={tr("光路图读取失败", "Could not read the layout")} />
        {layouts.length > 0 && (
          <button type="button" className="text-sm text-mast-accent underline" onClick={() => open(layouts[0]!.id, true)}>
            {tr("回到列表里的第一张", "Open the first layout of the list")}
          </button>
        )}
      </div>
    );
  } else if (doc.data) {
    body = <LayoutEditor key={doc.data.layout.id} initial={doc.data.layout} linksState={doc.data.linksState} taken={taken} onDirtyChange={setDirty} onSwitchTo={(n) => open(n, true)} />;
  }

  return (
    <>
      <Section
        title={tr("光路图", "Optical layout")}
        subtitle={tr("精确摆放器件（0.01 mm / 0.01°）、对准光束、给每个器件写注释，让人和 AI 围绕同一张光路图讨论", "Place components exactly (0.01 mm / 0.01°), align the beams, annotate every component, and let people and the AI discuss the same optical path")}
        actions={header}
      >
        {archiveMsg && <p className="mb-2 rounded-mast-ctl border border-mast-danger-border bg-mast-danger-bg px-3 py-1.5 text-sm text-mast-danger">{archiveMsg}</p>}
        {body}
      </Section>
      <NewLayoutDialog key={newOpen ? "open" : "closed"} open={newOpen} taken={taken} onClose={() => setNewOpen(false)} onCreate={create} />
    </>
  );
}
