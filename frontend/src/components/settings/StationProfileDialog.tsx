// Confirmation for switching the station profile (STM station <-> optical station).
//
// The profile is an admin-PIN setting (backend `admin_pin.GUARDED_KEYS`): it decides at the
// next start whether this process dials Nanonis and runs the tip-crash watchdog, and whether
// serial auto-detection probes every idle port. A mis-click costs nothing until the restart,
// which is why the click does not save. This dialog is the only way to save a choice:
//
//   1. it spells out what each profile means, what a wrong choice does, and that nothing
//      changes before the service restarts;
//   2. it asks for the admin PIN. The PIN is state of this component only: it goes out once,
//      with the confirm, and is cleared when the dialog closes or the backend refuses it.
//      Nothing here writes it to a store or to storage;
//   3. on a refusal (wrong PIN, no PIN set yet) the dialog stays open and shows the backend's
//      own reason, so "believing it saved" never happens (ConfirmDialog's contract).
//
// The wording and the rule for when the confirm button may be pressed are pure functions in
// lib/stationProfile.ts (confirmCopy, switchBlocker), read by test/stationProfile.test.ts.

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import clsx from "clsx";

import { api } from "@/api/client";
import { ConfirmDialog } from "@/components/scope/ConfirmDialog";
import { tr } from "@/lib/i18n";
import {
  confirmCopy,
  switchBlocker,
  type ProfileSource,
  type StationProfile,
} from "@/lib/stationProfile";

const PIN_INPUT =
  "w-full rounded-md border border-mast-border bg-mast-bg px-3 py-2 text-sm text-mast-text outline-none focus:border-mast-accent";

export function StationProfileDialog({
  open,
  from,
  to,
  source,
  busy,
  error,
  onConfirm,
  onClose,
}: {
  open: boolean;
  /** The profile saved now. */
  from: StationProfile;
  /** The profile being switched to. */
  to: StationProfile;
  source: ProfileSource;
  busy: boolean;
  /** The backend's reason when the last attempt was refused; the dialog stays open on it. */
  error: string | null;
  onConfirm: (pin: string) => void;
  onClose: () => void;
}) {
  const [pin, setPin] = useState("");

  // Same query key as the Advanced tab's PIN gate, so one request serves both.
  const status = useQuery({
    queryKey: ["admin", "pin-status"],
    queryFn: async () => {
      const { data, error: err } = await api.GET("/api/admin/pin-status");
      if (err) throw err;
      return data;
    },
    enabled: open,
  });
  const pinIsSet = status.data?.pin_is_set;

  // The PIN never outlives the dialog, and a refused PIN is not kept for the next try.
  useEffect(() => {
    if (!open) setPin("");
  }, [open]);
  useEffect(() => {
    if (error) setPin("");
  }, [error]);

  const copy = confirmCopy(from, to, source);
  const blocker = switchBlocker({ pin, pinIsSet, busy });

  return (
    <ConfirmDialog
      open={open}
      title={copy.title}
      confirmLabel={copy.confirmLabel}
      tone="danger"
      busy={busy}
      busyLabel={tr("保存中…", "Saving…")}
      disabled={blocker !== null}
      error={error}
      wide
      onConfirm={() => {
        if (blocker === null) onConfirm(pin);
      }}
      onClose={onClose}
    >
      <p className="text-sm text-mast-text">{copy.intro}</p>

      <div className="grid gap-3 sm:grid-cols-2">
        {copy.profiles.map((p) => (
          <div
            key={p.profile}
            data-profile={p.profile}
            data-selected={p.selected ? "true" : "false"}
            className={clsx(
              "rounded-mast-ctl border px-3 py-2 text-xs",
              p.selected
                ? "border-mast-warn-border bg-mast-warn-bg"
                : "border-mast-border bg-mast-panel-2",
            )}
          >
            <p className="font-semibold text-mast-text">
              {p.label}
              {p.selected ? tr("（要改成这个）", " (switching to this)") : ""}
            </p>
            <ul className="mt-1 list-disc space-y-1 pl-4 text-mast-muted">
              {p.lines.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </div>
        ))}
      </div>

      <p className="text-xs font-medium text-mast-warn" role="note">
        {copy.risk}
      </p>
      <p className="text-xs text-mast-muted">{copy.restart}</p>
      {copy.envNote && (
        <p className="text-xs text-mast-warn" role="note">
          {copy.envNote}
        </p>
      )}

      {pinIsSet === false && (
        <div
          role="alert"
          className="rounded border border-mast-warn/40 bg-mast-warn/10 px-3 py-2 text-sm text-mast-warn"
        >
          {tr(
            "这台机器还没有设置管理 PIN，站点画像是受 PIN 保护的设置，没有 PIN 就改不了。先到",
            "No admin PIN is set on this machine, and the station profile is a PIN-protected setting, so it cannot be changed yet. Set one first under ",
          )}
          <Link to="/settings/admin" className="mx-1 underline">
            {tr("设置 → 高级", "Settings → Advanced")}
          </Link>
          {tr(
            "（或桌面启动器的“设置高级管理密码”）设置一个。",
            " (or with the “Set admin password” button of the desktop launcher).",
          )}
        </div>
      )}

      <label className="flex flex-col gap-1 text-sm">
        <span className="text-mast-muted">{tr("管理 PIN", "Admin PIN")}</span>
        <input
          type="password"
          value={pin}
          autoComplete="off"
          autoFocus
          disabled={busy}
          placeholder={tr("保存需要管理 PIN", "The admin PIN is needed to save")}
          onChange={(e) => setPin(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && blocker === null) onConfirm(pin);
          }}
          className={PIN_INPUT}
        />
        <span className="text-xs text-mast-faint">
          {tr(
            "PIN 只随这一次保存发送，不会被存下来。",
            "The PIN is sent once with this save and is not stored.",
          )}
        </span>
      </label>
    </ConfirmDialog>
  );
}
