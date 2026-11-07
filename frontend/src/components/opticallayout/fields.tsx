import { useEffect, useId, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import { parseNumberInput } from "@/lib/operatorPrompts";

// Small form controls for the inspector. They keep a draft while the operator types and
// commit ONE change on Enter or when the box loses focus, so a typed number is one undo
// step and not one per keystroke; Escape puts the old value back. The draft follows the
// value when it changes from elsewhere (an arrow-key nudge, an undo).

export const INPUT_CLS =
  "w-full min-w-0 rounded-mast-ctl border border-mast-border-strong bg-mast-panel px-2 py-1 text-sm text-mast-text outline-none focus:border-mast-accent disabled:opacity-60";

export function Row({ label, hint, help, error, children, wide }: { label: ReactNode; hint?: ReactNode; /** longer explanation, shown as a tooltip on a small mark after the label */ help?: string; error?: string | null; children: ReactNode; wide?: boolean }) {
  const id = useId();
  return (
    <div className={clsx("grid items-start gap-x-2 gap-y-0.5", wide ? "grid-cols-1" : "grid-cols-[6.5rem_minmax(0,1fr)]")}>
      <label htmlFor={id} className="pt-1 text-xs text-mast-muted">
        {label}
        {help && (
          <span className="ml-1 cursor-help text-mast-faint" title={help} aria-label={help}>
            ⓘ
          </span>
        )}
      </label>
      <div className="min-w-0" id={id}>
        {children}
        {error ? <p className="mt-0.5 text-xs text-mast-danger">{error}</p> : hint ? <p className="mt-0.5 text-xs text-mast-faint">{hint}</p> : null}
      </div>
    </div>
  );
}

export function Group({ title, children, defaultOpen = true, badge }: { title: ReactNode; children: ReactNode; defaultOpen?: boolean; badge?: ReactNode }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="border-t border-mast-border first:border-t-0">
      <button type="button" className="flex w-full items-center justify-between py-2 text-left text-xs font-semibold uppercase tracking-wide text-mast-muted hover:text-mast-text" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span className="flex items-center gap-2">
          {title}
          {badge}
        </span>
        <span aria-hidden="true">{open ? "▾" : "▸"}</span>
      </button>
      {open && <div className="space-y-2 pb-3">{children}</div>}
    </section>
  );
}

export function CommitText({
  value,
  onCommit,
  placeholder,
  maxLength,
  mono,
  disabled,
  invalid,
  list,
  "aria-label": ariaLabel,
}: {
  value: string;
  onCommit: (v: string) => void;
  placeholder?: string;
  maxLength?: number;
  mono?: boolean;
  disabled?: boolean;
  invalid?: boolean;
  list?: string;
  "aria-label"?: string;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  const commit = () => {
    if (draft !== value) onCommit(draft);
  };
  return (
    <input
      type="text"
      value={draft}
      list={list}
      disabled={disabled}
      maxLength={maxLength}
      placeholder={placeholder}
      aria-label={ariaLabel}
      aria-invalid={invalid || undefined}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.currentTarget.blur();
        } else if (e.key === "Escape") {
          setDraft(value);
          e.currentTarget.blur();
        }
      }}
      className={clsx(INPUT_CLS, mono && "font-mono", invalid && "border-mast-danger")}
    />
  );
}

export function CommitTextarea({ value, onCommit, rows = 3, maxLength, placeholder, disabled }: { value: string; onCommit: (v: string) => void; rows?: number; maxLength?: number; placeholder?: string; disabled?: boolean }) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <textarea
      value={draft}
      rows={rows}
      maxLength={maxLength}
      disabled={disabled}
      placeholder={placeholder}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => draft !== value && onCommit(draft)}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          setDraft(value);
          e.currentTarget.blur();
        }
      }}
      className={clsx(INPUT_CLS, "resize-y")}
    />
  );
}

/** Shortest text that reads back as the same number: 112.5, not 112.50000000000001. */
export function numberText(v: number | null | undefined): string {
  return v === null || v === undefined || !Number.isFinite(v) ? "" : String(Number(v.toFixed(6)));
}

export interface NumberRules {
  min?: number;
  max?: number;
  /** `min` itself is not allowed */
  minExclusive?: boolean;
  /** an empty box clears the value (optional parameters) */
  optional?: boolean;
  /** arrow keys in the box add / subtract this (Shift: a tenth of it, Alt: ten times) */
  step?: number;
}

export function CommitNumber({
  value,
  onCommit,
  rules = {},
  unit,
  disabled,
  "aria-label": ariaLabel,
}: {
  value: number | null | undefined;
  /** null: the box was emptied (only if optional) */
  onCommit: (v: number | null) => void;
  rules?: NumberRules;
  unit?: string;
  disabled?: boolean;
  "aria-label"?: string;
}) {
  const [draft, setDraft] = useState(numberText(value));
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => {
    setDraft(numberText(value));
    setProblem(null);
  }, [value]);

  const apply = (text: string): boolean => {
    if (text.trim() === "") {
      if (rules.optional) {
        setProblem(null);
        if (value !== null && value !== undefined) onCommit(null);
        return true;
      }
      setProblem(tr("请输入一个数值。", "Enter a number."));
      return false;
    }
    const n = parseNumberInput(text);
    if (!n.ok) {
      setProblem(n.error);
      return false;
    }
    if ((rules.min !== undefined && (rules.minExclusive ? n.value <= rules.min : n.value < rules.min)) || (rules.max !== undefined && n.value > rules.max)) {
      const lo = rules.min !== undefined ? numberText(rules.min) : "…";
      const hi = rules.max !== undefined ? numberText(rules.max) : "…";
      setProblem(
        rules.minExclusive
          ? tr(`要大于 ${lo}、不超过 ${hi}。`, `Must be greater than ${lo} and at most ${hi}.`)
          : tr(`要在 ${lo} 到 ${hi} 之间。`, `Must be between ${lo} and ${hi}.`),
      );
      return false;
    }
    setProblem(null);
    if (n.value !== value) onCommit(n.value);
    return true;
  };

  const onKey = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.currentTarget.blur();
    } else if (e.key === "Escape") {
      setDraft(numberText(value));
      setProblem(null);
      e.currentTarget.blur();
    } else if ((e.key === "ArrowUp" || e.key === "ArrowDown") && rules.step) {
      e.preventDefault();
      const base = parseNumberInput(draft);
      const cur = base.ok ? base.value : (value ?? 0);
      const unitStep = rules.step * (e.shiftKey ? 0.1 : e.altKey ? 10 : 1);
      const next = Math.round((cur + (e.key === "ArrowUp" ? unitStep : -unitStep)) * 1e6) / 1e6;
      const text = numberText(next);
      setDraft(text);
      apply(text);
    }
  };

  return (
    <div>
      <div className="flex items-center gap-1.5">
        <input
          type="text"
          inputMode="decimal"
          value={draft}
          disabled={disabled}
          aria-label={ariaLabel}
          aria-invalid={problem !== null || undefined}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => {
            const n = parseNumberInput(draft);
            if (apply(draft) && n.ok) setDraft(numberText(n.value));
          }}
          onKeyDown={onKey}
          className={clsx(INPUT_CLS, "font-mono tabular-nums", problem && "border-mast-danger")}
        />
        {unit && <span className="shrink-0 text-xs text-mast-faint">{unit}</span>}
      </div>
      {problem && <p className="mt-0.5 text-xs text-mast-danger">{problem}</p>}
    </div>
  );
}

/** A labelled number box. */
export function NumberRow({
  label,
  hint,
  value,
  onCommit,
  rules,
  unit,
  disabled,
}: {
  label: ReactNode;
  hint?: ReactNode;
  value: number | null | undefined;
  onCommit: (v: number | null) => void;
  rules?: NumberRules;
  unit?: string;
  disabled?: boolean;
}) {
  return (
    <Row label={label} hint={hint}>
      <CommitNumber value={value} onCommit={onCommit} rules={rules} unit={unit} disabled={disabled} aria-label={typeof label === "string" ? label : undefined} />
    </Row>
  );
}
