// Confirmation dialog primitive on top of Modal, so callers do not hand-roll
// Cancel / Confirm buttons.
//
// Core contract: on failure the dialog stays open and renders the server's error
// inline. A toast is not enough: it disappears, and "believing it succeeded" is the
// costliest failure mode for these operations.

import type { ReactNode } from "react";
import { Button, Modal } from "../controls";
import { tr } from "@/lib/i18n";

export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel = tr("确认", "Confirm"),
  cancelLabel = tr("取消", "Cancel"),
  tone = "primary",
  busy = false,
  busyLabel,
  error,
  disabled = false,
  wide = false,
  onConfirm,
  onClose,
  extraAction,
}: {
  open: boolean;
  title: string;
  children?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "primary" | "danger";
  busy?: boolean;
  busyLabel?: string;
  /** Server error text. When non-empty it is shown inline and the dialog stays open. */
  error?: string | null;
  disabled?: boolean;
  wide?: boolean;
  onConfirm: () => void;
  onClose: () => void;
  /** Optional third action (e.g. "abort the task first"). */
  extraAction?: ReactNode;
}) {
  return (
    <Modal open={open} onClose={onClose} title={title} wide={wide}>
      <div className="space-y-4">
        {children}
        {error ? (
          <div
            role="alert"
            className="rounded border border-mast-danger/40 bg-mast-danger/10 px-3 py-2 text-sm text-mast-danger"
          >
            {error}
          </div>
        ) : null}
        <div className="flex items-center justify-end gap-2">
          {extraAction}
          <Button onClick={onClose} disabled={busy}>
            {cancelLabel}
          </Button>
          <Button
            variant={tone}
            onClick={onConfirm}
            loading={busy}
            disabled={busy || disabled}
          >
            {busy ? (busyLabel ?? tr("处理中…", "Working…")) : confirmLabel}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
