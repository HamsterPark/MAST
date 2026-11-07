import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import clsx from "clsx";
import { api } from "@/api/client";
import type { components } from "@/api/schema";
import { Badge, ErrorNote, Section, Spinner } from "@/components/ui";
import { Accordion, Button, Field, SelectField, TextField, useToast } from "@/components/controls";
import { SignalIndexField } from "@/components/signals/SignalIndexField";
import { SIGNAL_AUTO_VALUE, SIGNAL_INDEX_KEYS } from "@/lib/signalChannels";
import { INSTRUMENT_INIT_KEY, useInstrumentInit } from "@/hooks/useInstrumentInit";
import { splitBold } from "@/lib/inlineBold";
import { langTag, tr } from "@/lib/i18n";
import {
  groupsToOpen, isOutstanding, visibleItems, type InitItemLike,
} from "@/lib/initFilter";

// ── New instrument initialization ────────────────────────────────────────────
// Design doc: docs/v2/design/new_instrument_initialization.md
//
// This page answers one question: which numbers are still missing after
// installing on a new machine.
//
// The layout is deliberately a single scrollable page with groups rather than a
// next/back wizard: a wizard suits a first install but is a burden for "I only
// want to change one item", and this page serves both uses.
//
// The numbers entered here never pass through an LLM. A person types in an input
// box -> HTTP JSON -> Python validation -> existing store write -> Python
// read-back -> Python comparison. A tool call could send 3e-12 as 3 (off by 10^12),
// and the same model could then explain the read-back 3.0 as "= 3e-12, just floating
// point precision". So there is neither an "ask the AI to fill it in" path nor a
// step that hands two numbers to a model and asks whether they agree: the
// comparison is computed by the backend and shown row by row below.
//
// Catalog, ranges and enum choices are all fetched from the backend's source of
// truth (GET /api/instrument-init, items[].input). No range is copied into this
// file, since a copy would drift.

type InitItem = components["schemas"]["InitItemModel"];
type InitGroup = components["schemas"]["InitGroupModel"];
type InputSpec = components["schemas"]["InputSpec"];
type Verdict = components["schemas"]["ValueVerdict"];

const FALLBACK_INPUT: InputSpec = { type: "float", min: null, max: null, choices: [] };

/** The input spec comes from the backend; when absent fall back to a free-text box rather than guessing ranges. */
const inputOf = (item: InitItem): InputSpec => item.input ?? FALLBACK_INPUT;

// `tone` uses the BADGE_TONE key names of ui.tsx (AUTO / INFO / WARN / DANGEROUS);
// a wrong name silently falls back to default, turning a red "Required" into a
// neutral badge without any error.
const SEVERITY_META: Record<string, { label: string; tone: string; blurb: string }> = {
  required: {
    label: tr("必填", "Required"),
    tone: "DANGEROUS",
    blurb: tr(
      "缺了会让某条安全网失效，或者让一整类物理量整体错。",
      "Missing, it disables a safety net or makes a whole class of physical quantities wrong.",
    ),
  },
  recommended: {
    label: tr("推荐", "Recommended"),
    tone: "WARN",
    blurb: tr(
      "缺了功能会降级（并且会明说），但不会造成危险。",
      "Missing, features degrade (and say so), but nothing becomes dangerous.",
    ),
  },
  optional: {
    label: tr("可跳过", "Optional"),
    tone: "INFO",
    blurb: tr(
      "缺了只是少一句注释。系统不会因此发明一个数。",
      "Missing, only a note is lost. The system never invents a number for it.",
    ),
  },
};

/** Display form of a value. Scientific notation is shown as is; readers of this page want the magnitude. */
function fmtValue(v: unknown): string {
  if (v === null || v === undefined || v === "") return tr("（未设置）", "(not set)");
  if (typeof v === "number") {
    if (!Number.isFinite(v)) return String(v);
    if (v !== 0 && (Math.abs(v) < 1e-3 || Math.abs(v) >= 1e5)) return v.toExponential(4);
    return String(v);
  }
  if (Array.isArray(v)) return tr(`（${v.length} 项）`, `(${v.length} items)`);
  if (typeof v === "object") return tr("（已填写）", "(filled in)");
  return String(v);
}

/** Number typed into an input: notation such as `3e-12` is accepted verbatim, with no automatic rewriting. */
function parseNum(raw: string): number | null {
  const t = raw.trim();
  if (!t) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

/** "This number is several orders of magnitude above/below the allowed range" — a hint only, never blocking. The range comes from the backend. */
function magnitudeHint(item: InitItem, n: number | null): string {
  if (n === null || n === 0) return "";
  const { min, max } = inputOf(item);
  if (typeof max === "number" && max > 0 && n > max) {
    const d = Math.log10(n / max);
    return tr(
      `⚠ 比允许的上限大${d >= 1 ? ` 约 ${d.toFixed(1)} 个数量级` : ""}——保存时会被夹到上限，不会是你填的这个数。`,
      `⚠ Above the allowed maximum${d >= 1 ? ` by about ${d.toFixed(1)} orders of magnitude` : ""}; it will be clamped to the maximum on save and will not be the number you entered.`,
    );
  }
  if (typeof min === "number" && min > 0 && n < min) {
    const d = Math.log10(min / n);
    return tr(
      `⚠ 比允许的下限小${d >= 1 ? ` 约 ${d.toFixed(1)} 个数量级` : ""}——保存时会被夹到下限，不会是你填的这个数。`,
      `⚠ Below the allowed minimum${d >= 1 ? ` by about ${d.toFixed(1)} orders of magnitude` : ""}; it will be clamped to the minimum on save and will not be the number you entered.`,
    );
  }
  return "";
}

function SeverityBadge({ severity }: { severity: string }) {
  const meta = SEVERITY_META[severity] ?? SEVERITY_META.recommended!;
  return <Badge tone={meta.tone}>{meta.label}</Badge>;
}

/** Backend text marks emphasis with `**bold**`; without rendering it the asterisks show up inline. */
function Bold({ text }: { text: string | null | undefined }) {
  return (
    <>
      {splitBold(text).map((s, i) =>
        s.bold ? <b key={i} className="text-mast-text">{s.text}</b> : <span key={i}>{s.text}</span>,
      )}
    </>
  );
}

function StatusDot({ item }: { item: InitItem }) {
  const tone =
    item.status === "set" ? "bg-mast-auto"
      : item.status === "acknowledged" ? "bg-mast-accent"
        : item.status === "n/a" ? "bg-mast-border"
          : item.severity === "required" ? "bg-mast-danger"
            : "bg-mast-warn";
  const title =
    item.status === "set" ? tr("你填过一个明确的值", "You entered an explicit value")
      : item.status === "acknowledged" ? tr("你核对过：出厂值就是对的", "You checked it: the factory value is correct")
        : item.status === "n/a" ? tr("这台机器不适用", "Not applicable to this machine")
          : item.status === "missing" ? tr("没有出厂默认，也没填——硬缺口", "No factory default and nothing entered: a hard gap")
            : tr("还在用出厂默认，你没核对过", "Still using the factory default; not checked yet");
  return <span className={clsx("inline-block h-2 w-2 shrink-0 rounded-full", tone)} title={title} />;
}

/**
 * "What happens if this is wrong" — shown only for `safety_critical` items, and
 * always visible.
 *
 * The consequence text of the other items is not on the page; its source lives in
 * the comments above each item in `core/instrument_init.py`. It is always visible
 * rather than collapsed because the person filling in this item is probably
 * installing for the first time and the least likely to expand it. For example,
 * with `retract_motor_dir` reversed, retracting 3000 steps drives 3000 steps into
 * the sample.
 */
function ConsequenceLine({ item }: { item: InitItem }) {
  // Muted body, one warn-coloured marker. Painting fifteen required rows red would
  // be its own kind of noise, and the catalog puts its `**` on the reassuring half
  // as often as the scary one, so emphasis must not be wired to a danger colour.
  // Severity already has a badge.
  return (
    <p className="mt-1.5 text-xs leading-relaxed text-mast-muted">
      <span className="mr-1 text-mast-warn">⚠</span>
      <Bold text={item.consequence} />
    </p>
  );
}

// One-line hint: the entire explanation of an item on the page.
//
// No multi-paragraph card (what it is / where to find it / consequence /
// reconcilable): separate boxes invite filling every box with text. The backend
// composes a single `hint` field and the page shows one line.
//
// `probe` and `hint` refer to the same reading, so the "reconcilable" line is not
// shown; `probe` only feeds the "read from instrument" button.
function HintLine({ item }: { item: InitItem }) {
  if (!item.hint) return null;
  return (
    <p className="mt-1 text-xs leading-relaxed text-mast-muted">
      <Bold text={item.hint} />
    </p>
  );
}

// ── One row ──────────────────────────────────────────────────────────────────
function ItemRow({
  item, draft, onDraft, verdict,
}: {
  item: InitItem;
  draft: string | undefined;
  onDraft: (v: string | undefined) => void;
  verdict?: Verdict;
}) {
  // There is no collapsible "details" area: it was a container for redundant
  // explanations, and whenever the container exists someone adds another paragraph.
  // Each item shows only: label · (unit) · severity · one-line hint · (safety
  // items only) directional warning · input.
  //
  // This also saves a click: "where to read this number" used to hide in the
  // collapsed area, yet it is exactly what the person filling the form needs.
  const showConsequence = item.safety_critical;
  const spec = inputOf(item);
  const kind = spec.type;
  const current = draft ?? (item.value === null || item.value === undefined ? "" : String(item.value));
  const hint = kind === "float" || kind === "int" ? magnitudeHint(item, parseNum(current)) : "";

  return (
    <div className={clsx(
      "border-t border-mast-border py-3 first:border-t-0",
      item.status === "n/a" && "opacity-50",
    )}>
      <div className="flex flex-wrap items-center gap-2">
        <StatusDot item={item} />
        <span className="text-sm font-medium text-mast-text">{item.label}</span>
        {item.unit && <span className="text-xs text-mast-faint">（{item.unit}）</span>}
        <SeverityBadge severity={item.severity} />
      </div>

      {item.status !== "n/a" && <HintLine item={item} />}
      {showConsequence && item.status !== "n/a" && <ConsequenceLine item={item} />}

      {item.status === "n/a" ? (
        // A vague "one of the items above" leaves the reader with a 48-item puzzle,
        // so the backend names the item (instrument_init.na_reason) and the page
        // shows it as is.
        <p className="mt-1 text-xs text-mast-faint">
          {item.na_reason || tr("这台机器不适用。", "Not applicable to this machine.")}
        </p>
      ) : kind === "table" ? (
        <p className="mt-2 text-xs text-mast-muted">
          {tr("这一项在", "This item is edited in ")}
          <Link to="/settings/general" className="text-mast-accent hover:underline">
            {tr(" 设置 → 扫描参数档位 ", " Settings → Scan parameter tiers ")}
          </Link>
          {tr(
            "里编辑（那里是它的真源，`ScanAt` 每帧都从那里下发）。",
            " (the source of truth; `ScanAt` takes its values from there for every frame). ",
          )}
          {tr("当前：", "Current: ")}
          {item.value
            ? tr(`已自定义 ${(item.value as unknown[]).length} 档`, `${(item.value as unknown[]).length} tiers customized`)
            : tr("在用出厂 4 档模板", "using the factory 4-tier template")}
          {tr("。", ".")}
        </p>
      ) : kind === "modules" ? (
        <p className="mt-2 text-xs text-mast-muted">
          {tr("这一项在", "This item is ticked in ")}
          <Link to="/settings/general" className="text-mast-accent hover:underline">
            {tr(" 设置 → 硬件模块 ", " Settings → Hardware modules ")}
          </Link>
          {tr("里勾选（需要管理员 PIN）。", " (admin PIN required). ")}
          {tr("当前：", "Current: ")}
          {item.value
            ? tr("已声明过", "declared")
            : tr("从未声明 —— 全部按「没装」处理", "never declared; everything is treated as not installed")}
          {tr("。", ".")}
        </p>
      ) : SIGNAL_INDEX_KEYS.has(item.key) ? (
        // Signal indices are not entered as bare numbers. Dispatch is by key rather
        // than by InputSpec type: the options are live (the channel list read back
        // from the instrument), not the static enum in _CHOICE_SPEC, so the choice
        // path does not apply.
        <div className="mt-2 max-w-md">
          <SignalIndexField
            value={current}
            onCommit={(v) => onDraft(v)}
            autoValue={SIGNAL_AUTO_VALUE[item.key]}
            placeholder={item.factory !== null && item.factory !== undefined
              ? String(fmtValue(item.factory)) : tr("例如 86", "e.g. 86")}
          />
        </div>
      ) : kind === "choice" ? (
        <div className="mt-2 max-w-md">
          <SelectField
            value={current || String(item.factory ?? "")}
            onChange={(v) => onDraft(v)}
            options={[
              { value: "", label: tr("— 未选择 —", "— not selected —") },
              ...(spec.choices ?? []).map((c) => ({ value: c.value ?? "", label: c.label ?? "" })),
            ]}
          />
        </div>
      ) : (
        <div className="mt-2 max-w-md">
          {/* The input takes free text rather than <input type=number>: scientific
              notation (`3e-12`) must be typed verbatim, and number inputs may rewrite
              it in some browsers / IMEs. The point of this page is that a magnitude is
              never rewritten in transit. */}
          <TextField
            value={current}
            onChange={(v) => onDraft(v)}
            placeholder={item.factory !== null && item.factory !== undefined
              ? String(fmtValue(item.factory)) : tr("例如 1e9", "e.g. 1e9")}
            mono
          />
          <p className="mt-1 text-xs text-mast-faint">
            {item.factory !== null && item.factory !== undefined
              ? tr(`留空 = 用出厂默认 ${fmtValue(item.factory)}`, `Leave empty = use the factory default ${fmtValue(item.factory)}`)
              : tr(
                  "没有出厂默认——留空就是「未设置」，依赖它的功能会明说自己不可用。",
                  "No factory default: leaving it empty means \"not set\", and features that depend on it will say they are unavailable.",
                )}
          </p>
          {hint && <p className="mt-1 text-xs text-mast-danger">{hint}</p>}
        </div>
      )}

      {verdict && (
        <p className={clsx(
          "mt-2 rounded-mast-ctl border px-2.5 py-1.5 text-xs",
          verdict.verdict === "match"
            ? "border-mast-auto-border bg-mast-auto-bg text-mast-auto"
            : "border-mast-danger-border bg-mast-danger-bg text-mast-danger",
        )}>
          {verdict.verdict === "match"
            ? tr(`已写入并回读确认：${fmtValue(verdict.stored)}`, `Written and confirmed by read-back: ${fmtValue(verdict.stored)}`)
            : verdict.note}
        </p>
      )}
    </div>
  );
}

// ── One group ────────────────────────────────────────────────────────────────
function GroupCard({
  group, items, groupItems, drafts, setDraft, verdicts, onSave, onAcknowledge, busy,
  open, onOpenChange,
}: {
  group: InitGroup;
  /** The rows to DRAW — already filtered by to-do/all and the search box. */
  items: InitItem[];
  /** Every item in this group, filter or no filter. */
  groupItems: InitItem[];
  drafts: Record<string, string>;
  setDraft: (id: string, v: string | undefined) => void;
  verdicts: Record<string, Verdict>;
  onSave: (items: InitItem[]) => void;
  onAcknowledge: (items: InitItem[]) => void;
  busy: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [openIntro, setOpenIntro] = useState(false);
  // Counts and the SAVE SET come from the whole group, never the filtered view:
  // a draft typed before the operator changed the filter must still be written,
  // and a badge that shrinks with the filter is describing the filter, not the
  // machine.
  const outstanding = groupItems.filter((i) => !i.complete && i.severity === "required");
  const editable = groupItems.filter(
    (i) => inputOf(i).type !== "table" && inputOf(i).type !== "modules");
  const dirty = editable.some((i) => drafts[i.id] !== undefined);
  const defaulted = groupItems.filter((i) => i.status === "default" && i.severity !== "optional");
  const answered = groupItems.filter((i) => i.status === "set" || i.status === "acknowledged");
  const hidden = groupItems.length - items.length;

  // The intro is 2–6 lines of background per group. Show its first line and put
  // the rest behind a toggle: it explains why the group exists, which matters once
  // and then never again.
  const introLines = (group.intro || "").split("\n").filter((l) => l.trim());
  const introHead = introLines[0] ?? "";
  const introRest = introLines.slice(1);

  return (
    <Accordion
      open={open}
      onOpenChange={onOpenChange}
      title={
        <span className="flex flex-wrap items-center gap-2">
          <span>{group.title}</span>
          {outstanding.length > 0
            ? (
              <Badge tone="DANGEROUS">
                {tr(`${outstanding.length} 项必填未完成`, `${outstanding.length} required item(s) incomplete`)}
              </Badge>
            )
            : <Badge tone="AUTO">{tr("完成", "Done")}</Badge>}
          {/* A finished group collapses, and its contents then look like they do not
              exist (an operator could not find the retract direction to change it).
              Say what is inside, on the closed header. */}
          {answered.length > 0 && (
            <span className="text-xs font-normal text-mast-muted">
              {tr(`已填 ${answered.length} 项，可随时改`, `${answered.length} filled in; editable any time`)}
            </span>
          )}
        </span>
      }
    >
      <p className="mb-1 text-xs leading-relaxed text-mast-muted">
        <Bold text={introHead} />
        {introRest.length > 0 && (
          <button
            type="button"
            onClick={() => setOpenIntro((v) => !v)}
            className="ml-2 text-mast-accent hover:underline"
          >
            {openIntro ? tr("收起", "Less") : tr("更多", "More")}
          </button>
        )}
      </p>
      {openIntro && introRest.length > 0 && (
        <p className="mb-3 whitespace-pre-line text-xs leading-relaxed text-mast-muted">
          {introRest.map((l, i) => (
            <span key={i} className="block">
              <Bold text={l} />
            </span>
          ))}
        </p>
      )}
      {hidden > 0 && (
        <p className="mb-2 text-xs text-mast-faint">
          {tr(
            `当前筛选下显示 ${items.length} / ${groupItems.length} 项。`,
            `Showing ${items.length} / ${groupItems.length} items under the current filter.`,
          )}
        </p>
      )}
      <div className="mb-3" />
      <div>
        {items.map((it) => (
          <ItemRow
            key={it.id}
            item={it}
            draft={drafts[it.id]}
            onDraft={(v) => setDraft(it.id, v)}
            verdict={verdicts[it.id]}
          />
        ))}
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-mast-border pt-3">
        <Button variant="primary" disabled={!dirty || busy} loading={busy}
          onClick={() => onSave(editable)}>
          {tr("保存这一组并回读比对", "Save this group and verify by read-back")}
        </Button>
        {defaulted.length > 0 && (
          <Button disabled={busy} onClick={() => onAcknowledge(defaulted)}>
            {tr("这一组我核对过了（出厂值就对）", "I checked this group (factory values are correct)")}
          </Button>
        )}
        {defaulted.length > 0 && (
          <span className="text-xs text-mast-faint">
            {tr(
              `「核对过」标记 ${defaulted.length} 项还在用出厂默认的项。有默认的项，「没填」和「填的正好等于默认」在存储里是同一件事，程序分不出来——所以要你按一下。`,
              `"Checked" marks the ${defaulted.length} item(s) still using factory defaults. For items with a default, "not filled in" and "filled in with exactly the default" are the same thing in storage and the program cannot tell them apart, so you have to confirm.`,
            )}
          </span>
        )}
      </div>
    </Accordion>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────
export default function SetupPage() {
  const q = useInstrumentInit();
  const qc = useQueryClient();
  const { toast, node: toastNode } = useToast();
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [verdicts, setVerdicts] = useState<Record<string, Verdict>>({});
  const [pin, setPin] = useState("");
  const [openAbout, setOpenAbout] = useState(false);
  // To-do / all + search. The page serves two uses (first install / come back to
  // change one item); filled-in items used to move into finished, collapsed groups,
  // leaving no obvious entry point to change e.g. the retract direction.
  const [scope, setScope] = useState<"todo" | "all" | null>(null);
  const [query, setQuery] = useState("");
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});
  const [probeOut, setProbeOut] = useState<Record<string, unknown> | null>(null);
  const [imported, setImported] = useState<Record<string, unknown> | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const setDraft = (id: string, v: string | undefined) =>
    setDrafts((d) => {
      const next = { ...d };
      if (v === undefined) delete next[id];
      else next[id] = v;
      return next;
    });

  const invalidate = () => qc.invalidateQueries({ queryKey: INSTRUMENT_INIT_KEY });

  const applyMut = useMutation({
    mutationFn: async (payload: { store: string; values: Record<string, unknown> }) => {
      const { data, error } = await api.POST("/api/instrument-init/apply", {
        body: { ...payload, admin_pin: pin || null },
      });
      if (error) throw error;
      return data;
    },
  });

  const ackMut = useMutation({
    mutationFn: async (ids: string[]) => {
      const { data, error } = await api.POST("/api/instrument-init/acknowledge", {
        body: { item_ids: ids, undo: false },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      invalidate();
      toast(tr("已记下：这些项你核对过了。", "Recorded: you have checked these items."));
    },
  });

  const completeMut = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/instrument-init/complete", {
        body: { completed_by: "", rig_label: "" },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      invalidate();
      toast(tr(
        "已盖完成戳。换机器或重做标定后会自动重新提醒。",
        "Completion stamp set. You will be reminded again automatically after a machine change or recalibration.",
      ));
    },
  });

  // The Settings page has a "remind again" entry too, but that is not the one
  // people find. This clears the completion stamp without touching any value; the
  // required-item check never looks at the stamp.
  const reopenMut = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/instrument-init/reopen", {});
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      invalidate();
      toast(tr(
        "完成戳已清除：提醒横幅会回来。数值一个都没动。",
        "Completion stamp cleared: the reminder banner will return. No value was changed.",
      ));
    },
    onError: () => toast(tr("操作失败。", "Action failed."), "err"),
  });

  const probeMut = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/instrument-init/probe", {});
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      setProbeOut(d as unknown as Record<string, unknown>);
      invalidate();
      if (!d?.ok) toast(d?.message || tr("没能从仪器读到任何量。", "Could not read any quantity from the instrument."), "err");
    },
    onError: () => toast(tr("对账失败——检查 Nanonis 连接。", "Reconciliation failed; check the Nanonis connection."), "err"),
  });

  const importMut = useMutation({
    mutationFn: async (bundle: Record<string, unknown>) => {
      const { data, error } = await api.POST("/api/instrument-init/import", {
        body: { bundle },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: (d) => {
      if (!d?.ok) { toast(d?.rejected || tr("导入被拒绝。", "Import rejected."), "err"); return; }
      setImported(d.stores as Record<string, unknown>);
      // Importing fills numbers into the form; it does not write to hardware. Each
      // item goes into a draft and waits for per-group confirmation.
      const next: Record<string, string> = {};
      for (const [store, blob] of Object.entries(d.stores ?? {})) {
        if (store === "coarse_drive") continue;       // must be signed off again
        if (!blob || typeof blob !== "object") continue;
        for (const [k, v] of Object.entries(blob as Record<string, unknown>)) {
          if (v === null || v === undefined || typeof v === "object") continue;
          next[`${store}.${k}`] = String(v);
        }
      }
      setDrafts((cur) => ({ ...next, ...cur }));
      toast(tr(
        `已载入待复核（${Object.keys(next).length} 项）。逐组确认后才会写下去。`,
        `Loaded for review (${Object.keys(next).length} items). Nothing is written until you confirm group by group.`,
      ));
    },
    onError: () => toast(tr("导入失败。", "Import failed."), "err"),
  });

  const data = q.data;
  const allItems = useMemo(() => (data?.items ?? []) as InitItem[], [data]);

  // The default view follows the state of the machine: required gaps remain ->
  // to-do; setup finished -> all (someone coming back to change one item should
  // see everything, not a blank page).
  const effScope = scope ?? (data?.needs_setup ? "todo" : "all");
  const searching = query.trim().length > 0;

  const visible = useMemo(
    () => visibleItems(allItems as unknown as InitItemLike[], { scope: effScope, query }),
    [allItems, effScope, query],
  );
  const autoOpen = useMemo(() => groupsToOpen(visible, { query }), [visible, query]);

  const byGroup = useMemo(() => {
    const m: Record<string, InitItem[]> = {};
    for (const it of visible as unknown as InitItem[]) (m[it.group] ??= []).push(it);
    return m;
  }, [visible]);

  const allByGroup = useMemo(() => {
    const m: Record<string, InitItem[]> = {};
    for (const it of allItems) (m[it.group] ??= []).push(it);
    return m;
  }, [allItems]);

  const outstandingCount = allItems.filter((i) =>
    isOutstanding(i as unknown as InitItemLike)).length;

  // Manual open/close is per-view. Without this a group the operator collapsed
  // once would stay shut when a later search puts its only hit inside it, and the
  // search would look broken.
  useEffect(() => { setOpenGroups({}); }, [query, effScope]);

  const saveGroup = async (items: InitItem[]) => {
    const byStore: Record<string, Record<string, unknown>> = {};
    for (const it of items) {
      const raw = drafts[it.id];
      if (raw === undefined) continue;
      const t = raw.trim();
      // Empty = clear this key. The backend sanitizer ignores empty values, so the semantics agree.
      let val: unknown = t;
      const t2 = inputOf(it).type;
      if (t2 === "float" || t2 === "int") {
        const n = parseNum(t);
        if (t && n === null) {
          toast(tr(`${it.label}：这不是一个数。`, `${it.label}: this is not a number.`), "err");
          return;
        }
        val = n;
      }
      if (t === "") val = null;
      (byStore[it.store] ??= {})[it.key] = val;
    }
    if (!Object.keys(byStore).length) return;

    const nextVerdicts: Record<string, Verdict> = {};
    let anyBad = false;
    let restart = false;
    for (const [store, values] of Object.entries(byStore)) {
      try {
        const res = await applyMut.mutateAsync({ store, values });
        if (res?.pin_required) {
          toast(res.message || tr("这一组需要管理员 PIN。", "This group needs the admin PIN."), "err");
          return;
        }
        restart = restart || Boolean(res?.restart_required);
        for (const v of res?.verdicts ?? []) {
          nextVerdicts[`${store}.${v.key}`] = v as Verdict;
          if (v.verdict !== "match") anyBad = true;
        }
        if (!res?.ok && !res?.verdicts?.length) {
          anyBad = true;
          toast(res?.message || tr(`${store} 写入失败。`, `Writing ${store} failed.`), "err");
        }
      } catch {
        anyBad = true;
        toast(tr(`${store} 写入失败。`, `Writing ${store} failed.`), "err");
      }
    }
    setVerdicts((v) => ({ ...v, ...nextVerdicts }));
    if (!anyBad) {
      setDrafts((d) => {
        const next = { ...d };
        for (const it of items) delete next[it.id];
        return next;
      });
      toast(restart
        ? tr(
            "已保存。安全包络覆写要重启才生效——本进程仍在用旧值。",
            "Saved. The safety-envelope override only takes effect after a restart; this process still uses the old value.",
          )
        : tr("已保存并回读确认。", "Saved and confirmed by read-back."));
    } else {
      toast(tr(
        "有值回读回来和你填的不一样——看每一行下面的红字。",
        "Some read-back values differ from what you entered; see the red text under each row.",
      ), "err");
    }
    invalidate();
  };

  const doExport = async () => {
    const { data: d, error } = await api.GET("/api/instrument-init/export");
    if (error || !d?.ok) { toast(tr("导出失败。", "Export failed."), "err"); return; }
    const blob = new Blob([JSON.stringify(d.bundle, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `mast-instrument-init-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
    toast(d.stripped?.length
      ? tr(
          `已导出。剥掉了 ${d.stripped.length} 个学习量（它们绑这根针/这台机器，搬过去就是错值）。`,
          `Exported. ${d.stripped.length} learned quantities were stripped (they are tied to this tip / this machine and would be wrong elsewhere).`,
        )
      : tr("已导出。", "Exported."));
  };

  const onPickFile = (f: File | null) => {
    if (!f) return;
    const r = new FileReader();
    r.onload = () => {
      try {
        importMut.mutate(JSON.parse(String(r.result)));
      } catch {
        toast(tr("这个文件不是合法的 JSON。", "This file is not valid JSON."), "err");
      }
    };
    r.readAsText(f);
  };

  const pageTitle = tr("新仪器初始化", "New instrument setup");
  if (q.isLoading) return <Section title={pageTitle}><Spinner /></Section>;
  if (q.error) return <Section title={pageTitle}><ErrorNote error={q.error} /></Section>;
  if (!data) return null;

  const req = data.counts?.required ?? { total: 0, complete: 0 };
  const rec = data.counts?.recommended ?? { total: 0, complete: 0 };
  const busy = applyMut.isPending;

  return (
    <Section title={pageTitle}>
      {toastNode}

      {/* ── Top: progress + global actions ── */}
      <div className="sticky top-0 z-10 -mx-1 mb-4 rounded-mast-card border border-mast-border bg-mast-panel px-4 py-3 shadow-mast">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-sm">
            <b className={req.complete < req.total ? "text-mast-danger" : "text-mast-auto"}>
              {tr("必填", "Required")} {req.complete}/{req.total}
            </b>
            <span className="ml-3 text-mast-muted">{tr("推荐", "Recommended")} {rec.complete}/{rec.total}</span>
          </span>
          <div className="ml-auto flex flex-wrap gap-2">
            <Button onClick={() => probeMut.mutate()} loading={probeMut.isPending}>
              {tr("从仪器读一次对账", "Read from instrument and reconcile")}
            </Button>
            <Button onClick={doExport}>{tr("导出", "Export")}</Button>
            <Button onClick={() => fileRef.current?.click()} loading={importMut.isPending}>
              {tr("导入", "Import")}
            </Button>
            <input ref={fileRef} type="file" accept="application/json,.json" className="hidden"
              onChange={(e) => { onPickFile(e.target.files?.[0] ?? null); e.target.value = ""; }} />
            {data.completed_at ? (
              <Button loading={reopenMut.isPending} onClick={() => reopenMut.mutate()}>
                {tr("让提醒横幅回来", "Bring the reminder banner back")}
              </Button>
            ) : (
              <Button variant="primary" disabled={data.needs_setup}
                loading={completeMut.isPending}
                onClick={() => completeMut.mutate()}>
                {tr("全部完成，别再提醒", "All done, stop reminding")}
              </Button>
            )}
          </div>
        </div>
        {data.needs_setup && (
          <p className="mt-2 text-xs text-mast-danger">
            {tr(
              `还有 ${(data.outstanding_required ?? []).length} 项必填没有答案。`,
              `${(data.outstanding_required ?? []).length} required item(s) still have no answer. `,
            )}
            <b>{tr("「全部完成」按钮只抑制提醒，不豁免必填", "The \"All done\" button only suppresses the reminder; it does not waive required items")}</b>
            {tr(
              "——必填的判据永远从值本身算，所以它现在是禁用的。",
              ": the required check is always computed from the values themselves, so the button is disabled for now.",
            )}
          </p>
        )}
        {/* After completion the page used to say nothing, which looked like "nothing
            to do here" while the top banner disappeared too. The completion state
            stays on the page: who stamped it, when, which machine, and what exactly
            it suppresses. */}
        {!data.needs_setup && data.completed_at && (
          <p className="mt-2 text-xs text-mast-muted">
            <span className="text-mast-auto">{tr("已盖完成戳", "Completion stamp set")}</span>
            <span className="ml-1">
              （{new Date(data.completed_at * 1000).toLocaleString(langTag(), { hour12: false })}
              {data.rig_label ? ` · ${data.rig_label}` : ""}
              {data.rig_fingerprint ? ` · ${tr("指纹", "fingerprint")} ${data.rig_fingerprint}` : ""}）{tr("。", ".")}
            </span>
            <b className="ml-1 text-mast-text">{tr("戳只抑制横幅，不锁任何一项", "The stamp only suppresses the banner; it locks no item")}</b>
            {tr(
              "——下面每一项随时可以改，改完立刻回读比对。",
              ": every item below can be changed at any time and is verified by read-back right away.",
            )}
          </p>
        )}
        {data.fingerprint_changed && (
          <p className="mt-2 text-xs text-mast-warn">
            {tr(
              "硬件指纹与上次完成初始化时不同：要么换了机器，要么有人重做了标定。压电量程 / Z 限位 / 前放增益值得重新过一遍。",
              "The hardware fingerprint differs from the last completed setup: either the machine changed or someone recalibrated. Piezo range / Z limit / preamp gain are worth reviewing again.",
            )}
          </p>
        )}
        {data.degraded && (
          <p className="mt-2 text-xs text-mast-warn">
            {tr(
              "设置存储未接线——这份清单是只读的，填了不会保存。",
              "The settings store is not wired up; this list is read-only and entries will not be saved.",
            )}
          </p>
        )}
      </div>

      {/* ── What this page is for ──
          One line of conclusion plus one line of severity legend (a legend, not
          prose); everything else is collapsed. */}
      <div className="mb-4 rounded-mast-card border border-mast-border bg-mast-panel-2 p-4 text-xs leading-relaxed text-mast-muted">
        <p className="flex flex-wrap items-center gap-x-1 gap-y-1">
          <span>
            {tr("下面每一项都是", "Every item below is a number that ")}
            <b className="text-mast-text">{tr("「换一台仪器就会变成错的」", "becomes wrong when you change the instrument")}</b>
            {tr("的数。", ".")}
          </span>
          {Object.entries(SEVERITY_META).map(([k, m]) => (
            <span key={k} className="ml-1 inline-flex items-center gap-1" title={m.blurb}>
              <Badge tone={m.tone}>{m.label}</Badge>
            </span>
          ))}
          <button
            type="button"
            onClick={() => setOpenAbout((v) => !v)}
            className="ml-1 text-mast-accent hover:underline"
          >
            {openAbout ? tr("收起", "Less") : tr("分级与设计说明", "Severity levels and design notes")}
          </button>
        </p>
        {openAbout && (
          <div className="mt-2 space-y-2 border-t border-mast-border pt-2">
            <ul className="space-y-0.5">
              {Object.entries(SEVERITY_META).map(([k, m]) => (
                <li key={k}>
                  <Badge tone={m.tone}>{m.label}</Badge>
                  <span className="ml-1">{m.blurb}</span>
                </li>
              ))}
            </ul>
            <p>
              {tr(
                "这些数散在 6 个不同的存储里，默认值有三种完全不同的性质（出厂猜测 / 保守起点 / 根本没有默认）——这份清单就是把它们收在一处。",
                "These numbers are scattered across 6 different stores, and their defaults are of three quite different kinds (factory guess / conservative starting point / no default at all). This list gathers them in one place.",
              )}
            </p>
            <p>
              <b className="text-mast-text">{tr("这一页填的数字不经过任何 AI。", "The numbers entered on this page never pass through any AI.")}</b>
              {tr(
                "你打的字直接进代码、代码直接写、写完立刻读回来比对，结果逐行显示在下面。（模型有可能把 ",
                " What you type goes straight into code, the code writes it, reads it back and compares, and the result is shown row by row below. (A model could send ",
              )}
              <code>3e-12</code>
              {tr(" 发成 ", " as ")}
              <code>3</code>
              {tr("，随后还把读回的 ", ", and then describe the read-back ")}
              <code>3.0</code>
              {tr(
                " 说成「等于 3e-12，浮点精度而已」——所以比对绝不交给会犯错的那一方。）",
                " as \"equal to 3e-12, just floating-point precision\". So the comparison is never left to the party that can make the error.)",
              )}
            </p>
          </div>
        )}
      </div>

      {/* ── Preamp cross-check ── */}
      {data.preamp_check?.consistent === false && (
        <div className="mb-4 rounded-mast-card border border-mast-danger-border bg-mast-danger-bg p-3 text-xs text-mast-danger">
          {data.preamp_check.note}
        </div>
      )}
      {(data.derived ?? []).length > 0 && (
        <div className="mb-4 rounded-mast-card border border-mast-border bg-mast-panel p-4 text-xs">
          <p className="font-semibold text-mast-text">
            {tr("由前放满量程导出的两条下游线", "Two downstream limits derived from the preamp full scale")}
          </p>
          <p className="mt-1 text-mast-faint">
            {tr("这些是", "These are ")}
            <b>{tr("建议值，不会自动写入", "suggestions and are not written automatically")}</b>
            {tr(
              "。一条被程序悄悄改过的安全上限，就不再是你声明过的那条线——所以按不按由你。",
              ". A safety limit silently changed by the program is no longer the line you declared, so applying them is your call.",
            )}
          </p>
          <ul className="mt-2 space-y-2">
            {(data.derived ?? []).map((d) => (
              <li key={d.target} className="flex flex-wrap items-center gap-2">
                <code className="text-mast-muted">{d.target}</code>
                <span>{tr("建议", "Suggested")} <b className="font-mono">{fmtValue(d.value)}</b></span>
                <span className={clsx(d.current_is_looser ? "text-mast-danger" : "text-mast-faint")}>
                  {tr("当前", "Current")} {fmtValue(d.current)}
                  {d.current_is_looser && tr("（比物理量程还宽——这是危险方向）", " (wider than the physical range: the dangerous direction)")}
                </span>
                <Button
                  disabled={busy}
                  onClick={() => {
                    const dot = d.target.indexOf(".");
                    if (dot < 0) return;
                    void saveGroupDirect(d.target.slice(0, dot),
                      { [d.target.slice(dot + 1)]: d.value });
                  }}
                >
                  {tr("按建议收紧", "Tighten as suggested")}
                </Button>
                <span className="w-full text-mast-faint">{d.why}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ── Reconciliation result ── */}
      {probeOut && <ProbePanel probe={probeOut} />}

      {imported && (
        <div className="mb-4 rounded-mast-card border border-mast-warn-border bg-mast-warn-bg p-3 text-xs text-mast-warn">
          {tr("已载入一份导入包，值填在下面的输入框里但", "An import bundle was loaded; its values are in the inputs below but ")}
          <b>{tr("还没有写下去", "have not been written yet")}</b>
          {tr("。请逐组核对后按「保存这一组」。", ". Review them group by group, then press \"Save this group\".")}
          {Object.keys(imported).includes("coarse_drive") && (
            <b className="block mt-1">
              {tr(
                "粗动驱动电压没有被载入——那是唯一一个填错了叠堆当场报废的数，同型号也不等于同一台，必须重新签一次。",
                "The coarse drive voltage was not loaded: it is the one number that destroys the piezo stack on the spot if wrong, and the same model is not the same unit, so it must be signed off again.",
              )}
            </b>
          )}
        </div>
      )}

      {/* ── PIN ── */}
      <div className="mb-4 max-w-sm">
        <Field label={tr("管理员 PIN（只有粗动驱动电压 / 硬件模块需要）", "Admin PIN (only needed for coarse drive voltage / hardware modules)")}
          hint={tr(
            "没设 PIN 时这两项一律拒写（fail-closed）——不知道叠堆能承受多少，是不动的理由。",
            "With no PIN set, writes to these two items are always refused (fail-closed): not knowing how much the stack can take is a reason not to touch it.",
          )}>
          <input type="password" value={pin} onChange={(e) => setPin(e.target.value)}
            className="w-full rounded-mast-ctl border border-mast-border bg-mast-panel-2 px-3 py-2 text-sm" />
        </Field>
      </div>

      {/* ── View: to-do / all + search ──
          Fixes "no way to find the edit entry once filled in". Search is not
          affected by to-do/all: the item being looked for has by definition already
          been filled in. */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="inline-flex overflow-hidden rounded-mast-ctl border border-mast-border">
          {([
            { id: "todo", label: tr(`待办 (${outstandingCount})`, `To do (${outstandingCount})`) },
            { id: "all", label: tr(`全部 (${allItems.length})`, `All (${allItems.length})`) },
          ] as const).map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setScope(t.id)}
              className={clsx(
                "px-2.5 py-1 text-xs",
                effScope === t.id && !searching
                  ? "bg-mast-accent-soft font-medium text-mast-accent"
                  : "text-mast-muted hover:text-mast-text",
              )}
            >
              {t.label}
            </button>
          ))}
        </div>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={tr("搜索：退针 / preamp / z_extend_sign …", "Search: retract / preamp / z_extend_sign …")}
          className="min-w-[13rem] flex-1 rounded-mast-ctl border border-mast-border bg-mast-panel-2 px-3 py-1.5 text-sm text-mast-text placeholder:text-mast-faint"
        />
        {searching && (
          <>
            <span className="text-xs text-mast-muted">
              {tr(
                `全部 ${allItems.length} 项中命中 ${visible.length} 项（含已填）`,
                `${visible.length} of ${allItems.length} items match (including filled-in ones)`,
              )}
            </span>
            <button
              type="button"
              onClick={() => setQuery("")}
              className="text-xs text-mast-accent hover:underline"
            >
              {tr("清除", "Clear")}
            </button>
          </>
        )}
      </div>

      {/* ── Groups ── */}
      {(data.groups ?? []).map((g) => {
        const items = byGroup[g.id] ?? [];
        // A group with nothing left after the filter is not drawn; the case of
        // searching with zero hits overall gets its own message below.
        if (items.length === 0) return null;
        return (
          <GroupCard
            key={g.id}
            group={g}
            items={items}
            groupItems={allByGroup[g.id] ?? []}
            drafts={drafts}
            setDraft={setDraft}
            verdicts={verdicts}
            onSave={saveGroup}
            onAcknowledge={(its) => ackMut.mutate(its.map((i) => i.id))}
            busy={busy}
            open={openGroups[g.id] ?? autoOpen.has(g.id)}
            onOpenChange={(o) => setOpenGroups((s) => ({ ...s, [g.id]: o }))}
          />
        );
      })}

      {visible.length === 0 && (
        <p className="rounded-mast-card border border-mast-border bg-mast-panel-2 p-4 text-xs text-mast-muted">
          {searching
            ? tr(
                `没有匹配「${query.trim()}」的项。搜索会查标签、存储键名和说明文字。`,
                `No items match "${query.trim()}". Search covers labels, store key names and description text.`,
              )
            : tr(
                "没有待办项——这台机器该填的都填了。切到「全部」可以复查或修改任何一项。",
                "Nothing to do: everything this machine needs is filled in. Switch to \"All\" to review or change any item.",
              )}
        </p>
      )}
    </Section>
  );

  // Single-point write (used by "Tighten as suggested"). Goes through the same apply path, with the same read-back comparison.
  async function saveGroupDirect(store: string, values: Record<string, unknown>) {
    try {
      const res = await applyMut.mutateAsync({ store, values });
      const next: Record<string, Verdict> = {};
      for (const v of res?.verdicts ?? []) next[`${store}.${v.key}`] = v as Verdict;
      setVerdicts((cur) => ({ ...cur, ...next }));
      toast(
        res?.message || (res?.ok
          ? tr("已保存并回读确认。", "Saved and confirmed by read-back.")
          : tr("写入失败。", "Write failed.")),
        res?.ok ? "ok" : "err",
      );
      invalidate();
    } catch {
      toast(tr("写入失败。", "Write failed."), "err");
    }
  }
}

// ── Reconciliation result panel ──────────────────────────────────────────────
function ProbePanel({ probe }: { probe: Record<string, unknown> }) {
  const fields = (probe.fields ?? []) as {
    key: string; label: string; read: unknown; configured: unknown;
    verdict: string; note: string;
  }[];
  const warnings = (probe.warnings ?? []) as string[];
  return (
    <div className="mb-4 rounded-mast-card border border-mast-border bg-mast-panel p-4 text-xs">
      <p className="font-semibold text-mast-text">
        {tr("从仪器读回来的对账结果", "Reconciliation result read back from the instrument")}
      </p>
      <p className="mt-1 text-mast-faint">
        <b>{tr("只报告不一致，什么都没改。", "Only mismatches are reported; nothing was changed.")}</b>
        {tr(
          "哪个数对由你定；若要改，只往收紧的方向改是安全的。",
          " You decide which number is right; if you change one, only tightening is safe.",
        )}
      </p>
      {probe.rig_fingerprint ? (
        <p className="mt-1 text-mast-faint">
          {tr("硬件指纹 ", "Hardware fingerprint ")}<code>{String(probe.rig_fingerprint)}</code>
          {probe.fingerprint_changed ? tr("（与上次完成初始化时不同）", " (differs from the last completed setup)") : ""}
        </p>
      ) : null}
      {fields.length > 0 && (
        <div className="mt-2 overflow-x-auto">
          <table className="w-full min-w-[520px] text-left">
            <thead className="text-mast-faint">
              <tr>
                <th className="py-1">{tr("量", "Quantity")}</th>
                <th>{tr("仪器上读到", "Read from instrument")}</th>
                <th>{tr("MAST 登记", "Registered in MAST")}</th>
                <th>{tr("结论", "Result")}</th>
              </tr>
            </thead>
            <tbody className="font-mono tabular-nums">
              {fields.map((f) => (
                <tr key={f.key} className="border-t border-mast-border">
                  <td className="py-1 font-sans">{f.label}</td>
                  <td>{fmtValue(f.read)}</td>
                  <td>{fmtValue(f.configured)}</td>
                  <td className={clsx("font-sans",
                    f.verdict === "match" ? "text-mast-auto"
                      : f.verdict === "mismatch" ? "text-mast-danger" : "text-mast-muted")}>
                    {f.verdict === "match" ? tr("一致", "Match")
                      : f.verdict === "mismatch" ? tr("不一致", "Mismatch")
                        : f.verdict === "not_configured" ? tr("还没登记", "Not registered") : tr("没读到", "Not read")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {fields.some((f) => f.verdict === "mismatch") && (
        <ul className="mt-2 space-y-1 text-mast-danger">
          {fields.filter((f) => f.verdict !== "match" && f.note).map((f) => (
            <li key={f.key}>{f.label}{tr("：", ": ")}{f.note}</li>
          ))}
        </ul>
      )}
      {warnings.map((w, i) => (
        <p key={i} className="mt-2 text-mast-warn">{w}</p>
      ))}
    </div>
  );
}
