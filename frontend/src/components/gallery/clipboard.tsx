// Copy text to the clipboard.
//
// `navigator.clipboard` only exists in secure contexts: http://127.0.0.1 works, but opening MAST
// over http://<IP> on a LAN does not -- a common way to view data remotely -- so fall back to a
// temporary textarea + execCommand. If both fail the button says "Copy failed" instead of
// pretending to succeed.

import { useState } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";

export async function copyText(s: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(s);
    return true;
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = s;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }
}

export function CopyButton({
  text,
  label = tr("复制", "Copy"),
  className,
  title,
}: {
  text: string | (() => string);
  label?: string;
  className?: string;
  title?: string;
}) {
  const [state, setState] = useState<"" | "ok" | "fail">("");
  return (
    <button
      type="button"
      title={title}
      className={clsx(
        "rounded border border-mast-border px-1.5 text-xs text-mast-muted hover:border-mast-accent hover:text-mast-text",
        className,
      )}
      onClick={async (e) => {
        e.stopPropagation();
        const ok = await copyText(typeof text === "function" ? text() : text);
        setState(ok ? "ok" : "fail");
        setTimeout(() => setState(""), 1200);
      }}
    >
      {state === "ok" ? tr("已复制", "Copied") : state === "fail" ? tr("复制失败", "Copy failed") : label}
    </button>
  );
}
