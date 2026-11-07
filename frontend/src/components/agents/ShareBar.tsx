import { agentColorVar } from "@/components/agents/registry";
import type { Share } from "@/lib/contextInjection";
import { fmtChars } from "@/lib/contextInjection";

// A horizontal stacked bar plus legend. No chart library: the repo has none, and a
// single percentage bar does not justify one; the goal is seeing the largest share
// at a glance, not an interactive chart.
//
// Colors use only existing design tokens (`designTokens.test.ts` checks that every
// var(--…) is defined). The static prompt uses the agent's own identity color; the
// rest cycle through a fixed palette, since the number of blocks varies by version
// and a hard-coded one-color-per-block table would drift out of date.

const BLOCK_COLORS = [
  "var(--mast-info)",
  "var(--mast-accent)",
  "var(--mast-auto)",
  "var(--mast-warn)",
];

function colorOf(share: Share, agentId: string, blockIndex: number): string {
  if (share.key === "static") return agentColorVar(agentId);
  if (share.key === "history") return "var(--mast-muted)";
  if (share.key === "tools") return "var(--mast-border-strong)";
  if (share.key === "unattributed") return "var(--mast-faint)";
  return BLOCK_COLORS[blockIndex % BLOCK_COLORS.length] ?? "var(--mast-info)";
}

export function ShareBar({
  shares,
  agentId,
  caption,
}: {
  shares: Share[];
  agentId: string;
  caption?: string;
}) {
  if (shares.length === 0) return null;
  // Screen readers get the same information as text rather than just an image.
  const summary = shares.map((s) => `${s.label} ${s.pct}%`).join(" · ");
  let blockIndex = -1;
  const colored = shares.map((s) => {
    if (s.key === "block") blockIndex += 1;
    return { share: s, color: colorOf(s, agentId, blockIndex) };
  });

  return (
    <div>
      <div
        role="img"
        aria-label={summary}
        className="flex h-3 w-full overflow-hidden rounded-mast-badge border border-mast-border"
      >
        {colored.map(({ share, color }, i) => (
          <span
            key={`${share.key}-${share.id ?? i}`}
            title={`${share.label} — ${fmtChars(share.chars)}(${share.pct}%)`}
            style={{
              width: `${share.pct}%`,
              background: color,
              // Estimated segments are hatched so they are not mistaken for measured values.
              backgroundImage: share.estimated
                ? "repeating-linear-gradient(45deg, rgba(255,255,255,.35) 0 3px, transparent 3px 6px)"
                : undefined,
            }}
          />
        ))}
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-mast-muted">
        {colored.map(({ share, color }, i) => (
          <li key={`lg-${share.key}-${share.id ?? i}`} className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm border border-mast-border"
              style={{ background: color }}
            />
            <span className="text-mast-text">{share.label}</span>
            <span>{fmtChars(share.chars)} · {share.pct}%</span>
          </li>
        ))}
      </ul>
      {caption && <p className="mt-1.5 text-xs text-mast-faint">{caption}</p>}
    </div>
  );
}
