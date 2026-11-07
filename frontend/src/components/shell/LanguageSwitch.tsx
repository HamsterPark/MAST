import clsx from "clsx";
import { getLang, setLang, type Lang } from "@/lib/i18n";

// Language toggle shown in the top bar. Both choices stay visible with their own
// names so a reader of either language can find it.

const OPTIONS: { lang: Lang; label: string; title: string }[] = [
  { lang: "en", label: "EN", title: "English" },
  { lang: "zh", label: "中文", title: "简体中文" },
];

export function LanguageSwitch({ className }: { className?: string }) {
  const lang = getLang();
  return (
    <div
      role="group"
      aria-label="Language / 语言"
      title="Language / 语言"
      data-testid="language-switch"
      className={clsx(
        "inline-flex items-center gap-0.5 rounded-mast-ctl border border-mast-accent bg-mast-panel-2 p-0.5",
        className,
      )}
    >
      <span className="px-1 text-[12px] text-mast-muted" aria-hidden>
        🌐
      </span>
      {OPTIONS.map((o) => {
        const active = o.lang === lang;
        return (
          <button
            key={o.lang}
            type="button"
            onClick={() => setLang(o.lang)}
            aria-pressed={active}
            title={o.title}
            className={clsx(
              "rounded px-2 py-[3px] text-[11.5px] font-semibold transition-colors",
              active
                ? "bg-mast-accent text-white"
                : "text-mast-muted hover:text-mast-text",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
