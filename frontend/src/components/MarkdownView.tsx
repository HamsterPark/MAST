import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { tr } from "@/lib/i18n";

// Shared markdown renderer for reports, plans and paper drafts.
//
// Two constraints:
//   1. Wide content (tables, code blocks) scrolls horizontally inside its own
//      container; the page itself never scrolls horizontally.
//   2. Image links are relative paths (../_assets/x.png) the browser cannot
//      resolve, so images degrade to a visible one-line placeholder instead of a
//      broken icon. The exported HTML embeds images as base64.

export function MarkdownView({
  text,
  className = "",
}: {
  text: string;
  className?: string;
}) {
  return (
    <div className={`mast-md text-sm leading-relaxed text-mast-text ${className}`}>
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: (p) => <h1 className="mb-2 mt-4 text-lg font-semibold" {...p} />,
          h2: (p) => <h2 className="mb-2 mt-4 text-base font-semibold" {...p} />,
          h3: (p) => <h3 className="mb-1.5 mt-3 text-sm font-semibold" {...p} />,
          p: (p) => <p className="my-2" {...p} />,
          ul: (p) => <ul className="my-2 list-disc space-y-1 pl-5" {...p} />,
          ol: (p) => <ol className="my-2 list-decimal space-y-1 pl-5" {...p} />,
          blockquote: (p) => (
            <blockquote
              className="my-2 border-l-2 border-mast-border pl-3 text-mast-muted"
              {...p}
            />
          ),
          code: ({ className: cls, children, ...rest }) => {
            const inline = !String(cls ?? "").includes("language-");
            if (inline) {
              return (
                <code
                  className="rounded bg-mast-bg/60 px-1 py-0.5 font-mono text-[11px]"
                  {...rest}
                >
                  {children}
                </code>
              );
            }
            return (
              <code className={`font-mono text-[11px] ${cls ?? ""}`} {...rest}>
                {children}
              </code>
            );
          },
          pre: (p) => (
            <pre
              className="my-2 overflow-x-auto rounded-lg bg-mast-bg/60 p-3 text-[11px] leading-relaxed"
              {...p}
            />
          ),
          table: (p) => (
            <div className="my-2 overflow-x-auto">
              <table className="w-full border-collapse text-xs" {...p} />
            </div>
          ),
          th: (p) => (
            <th
              className="border border-mast-border bg-mast-bg/40 px-2 py-1 text-left font-medium"
              {...p}
            />
          ),
          td: (p) => <td className="border border-mast-border px-2 py-1" {...p} />,
          a: (p) => (
            <a
              className="text-mast-accent underline decoration-dotted"
              target="_blank"
              rel="noreferrer"
              {...p}
            />
          ),
          hr: () => <hr className="my-3 border-mast-border" />,
          // Relative-path images cannot load in the browser; say what the image is
          // and where to see it instead of showing a broken icon.
          img: ({ src, alt }) => (
            <span className="my-2 flex items-center gap-2 rounded border border-dashed border-mast-border px-2 py-1.5 text-[11px] text-mast-muted">
              <span aria-hidden>🖼</span>
              <span>
                {tr("图：", "Figure: ")}{alt || tr("(无说明)", "(no caption)")}
                <span className="ml-1 font-mono opacity-70">{src}</span>
                <span className="ml-1">{tr("— 导出 HTML 可看到内嵌图片", "— export HTML to see embedded images")}</span>
              </span>
            </span>
          ),
        }}
      >
        {text}
      </Markdown>
    </div>
  );
}
