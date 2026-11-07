// Toast for the data gallery.
//
// A page-level store rather than a per-component useToast: the bottom bar, the Marked page and
// the build panel all need toasts, and a toast must outlive the component that raised it (for
// example the view navigates away right after "Saved as series").

import { useEffect } from "react";
import type { ReactNode } from "react";
import { create } from "zustand";
import clsx from "clsx";

interface ToastState {
  msg: { body: ReactNode; tone: "ok" | "err"; seq: number } | null;
  show: (body: ReactNode, tone?: "ok" | "err") => void;
  hide: () => void;
}

let seq = 0;

export const useGalleryToast = create<ToastState>()((set) => ({
  msg: null,
  show: (body, tone = "ok") => set({ msg: { body, tone, seq: ++seq } }),
  hide: () => set({ msg: null }),
}));

export function toast(body: ReactNode, tone: "ok" | "err" = "ok"): void {
  useGalleryToast.getState().show(body, tone);
}

export function GalleryToast() {
  const msg = useGalleryToast((s) => s.msg);
  const hide = useGalleryToast((s) => s.hide);
  useEffect(() => {
    if (!msg) return;
    const t = setTimeout(hide, 4000);
    return () => clearTimeout(t);
  }, [msg, hide]);
  if (!msg) return null;
  return (
    <div
      role="status"
      className={clsx(
        "fixed bottom-24 left-1/2 z-[70] max-w-[90vw] -translate-x-1/2 rounded-mast-ctl border px-4 py-2 text-sm shadow-mast",
        msg.tone === "ok"
          ? "border-mast-border-strong bg-mast-text text-mast-bg [&_a]:underline"
          : "border-mast-danger-border bg-mast-danger-bg text-mast-danger",
      )}
    >
      {msg.body}
    </div>
  );
}
