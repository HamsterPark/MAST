import { useState } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import { Modal } from "@/components/controls";
import { parseNumberInput } from "@/lib/operatorPrompts";
import type { TableSpec } from "@/lib/opticalLayout/types";
import { LIMITS } from "@/lib/opticalLayout/types";
import { DEFAULT_TABLE } from "@/lib/opticalLayout/edit";
import { layoutIdProblem } from "@/lib/opticalLayout/saveFlow";
import type { Conflict } from "@/lib/opticalLayout/editorState";
import { INPUT_CLS, Row } from "./fields";

// New layout, save as a new layout, and the banner for a refused save (409).

interface Preset {
  id: string;
  label: () => string;
  table: TableSpec;
}

const PRESETS: Preset[] = [
  { id: "metric", label: () => tr("公制面包板 1200 × 900 mm，孔距 25 mm", "Metric breadboard 1200 × 900 mm, 25 mm pitch"), table: DEFAULT_TABLE },
  { id: "imperial", label: () => tr("英制面包板 4 × 3 ft，孔距 1 in", "Imperial breadboard 4 × 3 ft, 1 in pitch"), table: { width_mm: 1219.2, height_mm: 914.4, hole_pitch_mm: 25.4, hole_offset_mm: 12.7, grid: "metric" } },
  { id: "small", label: () => tr("小面包板 600 × 450 mm，孔距 25 mm", "Small breadboard 600 × 450 mm, 25 mm pitch"), table: { ...DEFAULT_TABLE, width_mm: 600, height_mm: 450 } },
  { id: "custom", label: () => tr("自定义…", "Custom…"), table: DEFAULT_TABLE },
];

const TABLE_RANGE = {
  width_mm: [LIMITS.tableSizeMin, LIMITS.tableSizeMax],
  height_mm: [LIMITS.tableSizeMin, LIMITS.tableSizeMax],
  hole_pitch_mm: [LIMITS.pitchMin, LIMITS.pitchMax],
  hole_offset_mm: [0, LIMITS.holeOffsetMax],
} as const;

function TableFields({ table, onChange }: { table: TableSpec; onChange: (t: TableSpec) => void }) {
  const field = (key: "width_mm" | "height_mm" | "hole_pitch_mm" | "hole_offset_mm", label: string) => (
    <Row label={label}>
      <input
        className={clsx(INPUT_CLS, "font-mono")}
        inputMode="decimal"
        defaultValue={String(table[key])}
        key={`${key}-${table[key]}`}
        onBlur={(e) => {
          const n = parseNumberInput(e.target.value);
          if (n.ok && n.value >= TABLE_RANGE[key][0] && n.value <= TABLE_RANGE[key][1]) onChange({ ...table, [key]: n.value });
          else e.target.value = String(table[key]);
        }}
        aria-label={label}
      />
    </Row>
  );
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
      {field("width_mm", tr("宽 (mm)", "Width (mm)"))}
      {field("height_mm", tr("高 (mm)", "Height (mm)"))}
      {field("hole_pitch_mm", tr("孔距 (mm)", "Hole pitch (mm)"))}
      {field("hole_offset_mm", tr("首孔偏移 (mm)", "First hole at (mm)"))}
    </div>
  );
}

const PRIMARY = "rounded-mast-ctl bg-mast-accent px-3.5 py-1.5 text-sm font-semibold text-mast-accent-ink hover:opacity-90 disabled:opacity-50";
const SECONDARY = "rounded-mast-ctl border border-mast-border-strong px-3.5 py-1.5 text-sm text-mast-text hover:bg-mast-panel-2";

export function NewLayoutDialog({ open, taken, onClose, onCreate }: { open: boolean; taken: ReadonlySet<string>; onClose: () => void; onCreate: (id: string, title: string, table: TableSpec) => void }) {
  const [id, setId] = useState("");
  const [title, setTitle] = useState("");
  const [preset, setPreset] = useState("metric");
  const [table, setTable] = useState<TableSpec>(DEFAULT_TABLE);
  const problem = id === "" ? null : layoutIdProblem(id, taken);
  return (
    <Modal open={open} onClose={onClose} title={tr("新建光路图", "New layout")}>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (layoutIdProblem(id, taken) === null) onCreate(id, title.trim() || id, table);
        }}
      >
        <Row label={tr("编号", "Id")} hint={tr("字母、数字、下划线、连字符；用作文件名。", "Letters, digits, underscore, hyphen; used as the file name.")} error={problem}>
          <input className={clsx(INPUT_CLS, "font-mono")} value={id} onChange={(e) => setId(e.target.value)} maxLength={40} autoFocus placeholder="shg-main" aria-label={tr("编号", "Id")} />
        </Row>
        <Row label={tr("标题", "Title")}>
          <input className={INPUT_CLS} value={title} onChange={(e) => setTitle(e.target.value)} placeholder={tr("例如：SHG 激发光路", "e.g. SHG excitation path")} aria-label={tr("标题", "Title")} />
        </Row>
        <Row label={tr("台面", "Platform")}>
          <select
            className={INPUT_CLS}
            value={preset}
            onChange={(e) => {
              setPreset(e.target.value);
              const p = PRESETS.find((x) => x.id === e.target.value);
              if (p && p.id !== "custom") setTable(p.table);
            }}
            aria-label={tr("台面预设", "Platform preset")}
          >
            {PRESETS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label()}
              </option>
            ))}
          </select>
        </Row>
        <TableFields table={table} onChange={(t) => { setTable(t); setPreset("custom"); }} />
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" className={SECONDARY} onClick={onClose}>
            {tr("取消", "Cancel")}
          </button>
          <button type="submit" className={PRIMARY} disabled={id === "" || problem !== null}>
            {tr("创建", "Create")}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function SaveAsDialog({ open, suggested, taken, busy, error, onClose, onSave }: { open: boolean; suggested: string; taken: ReadonlySet<string>; busy: boolean; error: string | null; onClose: () => void; onSave: (id: string, title: string) => void }) {
  const [id, setId] = useState(suggested);
  const [title, setTitle] = useState("");
  const problem = layoutIdProblem(id, taken);
  return (
    <Modal open={open} onClose={onClose} title={tr("另存为新的光路图", "Save as a new layout")}>
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (problem === null) onSave(id, title);
        }}
      >
        <p className="text-sm text-mast-muted">{tr("用现在画布上的内容存成一张新图，原来的那张不动。", "Saves what is on the canvas as a new layout and leaves the original alone.")}</p>
        <Row label={tr("新编号", "New id")} error={problem}>
          <input className={clsx(INPUT_CLS, "font-mono")} value={id} onChange={(e) => setId(e.target.value)} maxLength={40} autoFocus aria-label={tr("新编号", "New id")} />
        </Row>
        <Row label={tr("标题", "Title")} hint={tr("留空则沿用原标题", "Leave empty to keep the title")}>
          <input className={INPUT_CLS} value={title} onChange={(e) => setTitle(e.target.value)} aria-label={tr("标题", "Title")} />
        </Row>
        {error && <p className="text-sm text-mast-danger">{error}</p>}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" className={SECONDARY} onClick={onClose}>
            {tr("取消", "Cancel")}
          </button>
          <button type="submit" className={PRIMARY} disabled={problem !== null || busy}>
            {busy ? tr("保存中…", "Saving…") : tr("另存", "Save")}
          </button>
        </div>
      </form>
    </Modal>
  );
}

/** Shown above the canvas after a save was refused with 409. */
export function ConflictBanner({ conflict, baseRevision, busy, onLoadLatest, onSaveAs, onKeep }: { conflict: Conflict; baseRevision: number; busy: boolean; onLoadLatest: () => void; onSaveAs: () => void; onKeep: () => void }) {
  return (
    <div role="alert" className="rounded-mast-ctl border border-mast-warn-border bg-mast-warn-bg p-3 text-sm text-mast-text" data-banner="conflict">
      <p className="font-semibold text-mast-warn">{tr("别处已经改过这张光路图", "This layout was changed elsewhere")}</p>
      <p className="mt-1 text-mast-text">
        {conflict.currentRevision !== null
          ? tr(`服务器上已经是第 ${conflict.currentRevision} 版，你是在第 ${baseRevision} 版上改的。你的修改还在这里，没有丢。`, `The server is at revision ${conflict.currentRevision}; you started from revision ${baseRevision}. Your edits are still here and not lost.`)
          : tr("另一个标签页或另一个人先保存了。你的修改还在这里，没有丢。", "Another tab or another person saved first. Your edits are still here and not lost.")}
        {conflict.updatedBy ? tr(` 最近一次保存是 ${conflict.updatedBy}${conflict.updatedAt ? `（${conflict.updatedAt.slice(0, 16).replace("T", " ")}）` : ""}。`, ` The latest save was by ${conflict.updatedBy}${conflict.updatedAt ? ` (${conflict.updatedAt.slice(0, 16).replace("T", " ")})` : ""}.`) : null}
      </p>
      <div className="mt-2 flex flex-wrap gap-2">
        <button type="button" disabled={busy} className="rounded-mast-ctl bg-mast-accent px-3 py-1 text-sm font-semibold text-mast-accent-ink hover:opacity-90 disabled:opacity-50" onClick={onLoadLatest} title={tr("载入服务器上的最新版；你现在的修改可以用 Ctrl+Z 找回", "Loads the newest revision; Ctrl+Z brings your current edits back")}>
          {tr("载入最新版", "Load the newest")}
        </button>
        <button type="button" disabled={busy} className="rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-3 py-1 text-sm text-mast-text hover:bg-mast-panel-2 disabled:opacity-50" onClick={onSaveAs}>
          {tr("另存为新图", "Save as new")}
        </button>
        <button type="button" className="rounded-mast-ctl px-3 py-1 text-sm text-mast-muted hover:text-mast-text" onClick={onKeep}>
          {tr("先不处理，继续编辑", "Keep editing")}
        </button>
      </div>
    </div>
  );
}
