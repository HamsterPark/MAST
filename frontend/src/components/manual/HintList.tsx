// What each button actually does (the hint from the device description: the real effect of the
// vendor call). Stop-type hints are always shown, e.g. "stops all motor groups, including the STM
// coarse group"; the rest are collapsed under "Button notes". Each button also shows its hint on hover.

import { tr } from "@/lib/i18n";

export function HintList({ hints }: { hints: ReadonlyArray<{ label: string; hint: string; stop: boolean }> }) {
  if (!hints.length) return null;
  const stops = hints.filter((h) => h.stop);
  const rest = hints.filter((h) => !h.stop);
  return (
    <div className="mt-2 space-y-0.5 text-[10.5px] text-mast-faint">
      {stops.map((h) => (
        <div key={`${h.label}|${h.hint}`}>
          {h.label}{tr("：", ": ")}{h.hint}
        </div>
      ))}
      {rest.length > 0 && (
        <details>
          <summary className="cursor-pointer select-none hover:text-mast-text">{tr(`按钮说明（${rest.length}）`, `Button notes (${rest.length})`)}</summary>
          <ul className="mt-0.5 space-y-0.5 pl-3">
            {rest.map((h) => (
              <li key={`${h.label}|${h.hint}`}>
                {h.label}{tr("：", ": ")}{h.hint}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
