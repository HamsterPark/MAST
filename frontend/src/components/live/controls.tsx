// Small controls for the live-chart toolbar. A size more compact than RadioGroup
// in the shared controls (a single row above the chart), and an option can be
// disabled (e.g. cm⁻¹ when there is no excitation line). A disabled option is
// still shown, with an explanation, rather than removed: an option that cannot be
// found tends to be reported as a missing feature.

import type { ReactNode } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";

export function Segmented<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T;
  onChange: (v: T) => void;
  options: { value: T; label: string; disabled?: boolean; title?: string }[];
}) {
  return (
    <div className="inline-flex overflow-hidden rounded-mast-ctl border border-mast-border" role="radiogroup">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          disabled={o.disabled}
          title={o.title}
          onClick={() => onChange(o.value)}
          className={clsx(
            "px-2.5 py-1 text-xs",
            value === o.value
              ? "bg-mast-accent-soft font-medium text-mast-accent"
              : "text-mast-muted hover:text-mast-text",
            o.disabled && "cursor-not-allowed opacity-50 hover:text-mast-muted",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function ToggleChip({
  on,
  onClick,
  children,
  title,
}: {
  on: boolean;
  onClick: () => void;
  children: ReactNode;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={on}
      title={title}
      className={clsx(
        "rounded-mast-ctl border px-2.5 py-1 text-xs",
        on
          ? "border-mast-accent bg-mast-accent-soft text-mast-accent"
          : "border-mast-border text-mast-muted hover:text-mast-text",
      )}
    >
      {children}
    </button>
  );
}

/**
 * Channel legend: colour swatch + name + value (text uses the text colour, not the
 * data colour). Click to hide / show the channel. In the light theme several
 * palette colours have less than 3:1 contrast against the background, so the name
 * and value must be text: channels are identified here, not by colour.
 */
export function ChannelLegend({
  channels,
  colors,
  hidden,
  onToggle,
  values,
}: {
  channels: string[];
  colors: string[];
  hidden: Set<string>;
  onToggle: (name: string) => void;
  values: (string | null)[];
}) {
  if (!channels.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs">
      {channels.map((name, i) => {
        const off = hidden.has(name);
        return (
          <button
            key={name}
            type="button"
            onClick={() => onToggle(name)}
            aria-pressed={!off}
            title={off ? tr("点击显示", "Click to show") : tr("点击隐藏", "Click to hide")}
            className={clsx(
              "inline-flex items-center gap-1.5 rounded-mast-ctl border px-2 py-0.5",
              off ? "border-dashed border-mast-border text-mast-faint" : "border-mast-border text-mast-text",
            )}
          >
            <span
              aria-hidden
              className="inline-block h-[3px] w-4 rounded-full"
              style={{ background: off ? "transparent" : colors[i], outline: off ? "1px solid currentColor" : undefined }}
            />
            <span className="font-mono">{name}</span>
            {values[i] !== null && values[i] !== undefined && (
              <span className="font-mono tabular-nums text-mast-muted">{values[i]}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
