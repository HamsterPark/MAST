// Small gallery pieces: tag chips, key caps, rating-button colours, and the readable reasons for undecided criteria.
//
// Colours always come from --mast-* tokens: atomic = auto, superstructure = dream,
// incomplete/duplicate/fragment = warn, new = info, series = accent, anchored = info;
// ★ = warn, ✓ = auto, ✗ = danger. The lightbox always has a dark background, so it has its own dark* constants.

import type { ReactNode } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";

export type ChipKind =
  | "new" | "atom" | "half" | "part" | "dup" | "seg" | "copies" | "gridpart" | "li"
  | "user" | "ser" | "anc";

const CHIP: Record<ChipKind, string> = {
  new: "bg-mast-info-bg text-mast-info",
  atom: "bg-mast-auto-bg text-mast-auto",
  half: "bg-[color-mix(in_srgb,var(--mast-dream)_16%,transparent)] text-mast-dream",
  part: "bg-mast-warn-bg text-mast-warn",
  dup: "bg-mast-warn-bg text-mast-warn",
  seg: "bg-mast-warn-bg text-mast-warn",
  gridpart: "bg-mast-warn-bg text-mast-warn",
  copies: "border border-mast-border bg-mast-panel-2 text-mast-muted",
  li: "bg-mast-auto-bg text-mast-auto",
  user: "border border-mast-border bg-mast-panel-2 text-mast-text",
  ser: "bg-mast-accent-soft text-mast-accent hover:underline",
  anc: "bg-mast-info-bg text-mast-info",
};

export function chipClass(kind: ChipKind, extra?: string): string {
  return clsx(
    "mr-[3px] mt-px inline-block rounded-[2px] px-1 font-sans text-[10.5px] leading-[1.5]",
    CHIP[kind],
    extra,
  );
}

export function Chip({ kind, title, children }: { kind: ChipKind; title?: string; children: ReactNode }) {
  return (
    <span className={chipClass(kind)} title={title}>
      {children}
    </span>
  );
}

export function Kbd({ children, dark }: { children: ReactNode; dark?: boolean }) {
  return (
    <kbd className={clsx("ml-[3px] font-mono text-[10px]", dark ? "text-[#9aa3ad]" : "text-mast-faint")}>
      {children}
    </kbd>
  );
}

/** Colours of the pressed state of the three small ✓★✗ buttons on a card. */
export function ratingOnClass(r: number): string {
  if (r === 2) return "border-mast-warn bg-mast-warn text-white";
  if (r === 1) return "border-mast-auto bg-mast-auto text-white";
  return "border-mast-danger bg-mast-danger text-white";
}

/** Pressed-state colours of the rating buttons in the lightbox sidebar (dark background). */
export function darkRatingOnClass(r: number): string {
  if (r === 2) return "border-[#f0b545] bg-[#b7791f]";
  if (r === 1) return "border-[#5fc39d] bg-[#1d7a5f]";
  return "border-[#e58585] bg-[#8b2c2c]";
}

export const DARK_BTN =
  "rounded-[3px] border border-[#444] bg-[#2a2e33] px-[9px] py-[3px] text-[#eee] hover:border-[#a897ee] disabled:opacity-50";

export const SMALL_SELECT =
  "rounded-[3px] border border-mast-border bg-mast-panel px-1 py-0.5 text-[13px] text-mast-text";

export const SMALL_BTN =
  "rounded-[3px] border border-mast-border bg-mast-panel px-[7px] py-px text-[13px] text-mast-text hover:border-mast-accent disabled:opacity-50";

/** Reasons the atomic-phase criterion could not decide (reasons from vision/atomic_phase.py) -> human-readable text. */
const AR_TEXT: Record<string, string> = {
  scale_gate: tr("像素太粗（> 0.05 nm/px），这个尺度上晶格分辨不出来", "Pixels too coarse (> 0.05 nm/px); the lattice cannot be resolved at this scale"),
  scale_reduced: tr("0.02–0.05 nm/px 过渡带，证据强度不够下正面结论", "0.02–0.05 nm/px transition band; evidence too weak for a positive conclusion"),
  unknown_pixel_size: tr("不知道像素尺寸", "Pixel size unknown"),
  incomplete_frame: tr("帧不完整", "Incomplete frame"),
  insufficient_data: tr("有效数据太少", "Too little valid data"),
  too_few_periods: tr("视野里装不下足够多的周期", "Field of view holds too few periods"),
  dead_flat: tr("图像几乎是平的", "Image is almost flat"),
  dependency_unavailable: tr("判据依赖不可用", "Criterion dependency unavailable"),
  too_few_rows: tr("有效行太少", "Too few valid rows"),
};

export function arText(ar: string | null | undefined): string {
  if (!ar) return "";
  return AR_TEXT[ar] ? `${AR_TEXT[ar]}${tr(`（${ar}）`, ` (${ar})`)}` : ar;
}
