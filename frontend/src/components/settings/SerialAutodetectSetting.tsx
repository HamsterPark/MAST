// Settings → Thermometer / vacuum gauge: serial auto-detection on stations where it is not
// simply "as configured" (generic_station.md §1.4 / §4.2; pure logic in lib/serialAutodetect.ts).
//
//   * optical (`forced_off`): the state and why, no switch;
//   * generic (`default_off`): the state, why, and a switch. The setting `serial_autodetect` is
//     an admin-PIN setting (backend GUARDED_KEYS): switching it on makes MAST probe every idle
//     serial port, and that shows only at the next start or rescan. So, like the station
//     profile, a click only asks; the confirmation takes the PIN, and its confirm button is the
//     one place that writes the key;
//   * STM (`default_on`) and a service that does not say: nothing at all, no request. The STM
//     Settings page does not change by a pixel.

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/api/client";
import { SetPinForm } from "@/components/admin/PinGate";
import { Toggle } from "@/components/controls";
import { ConfirmDialog } from "@/components/scope/ConfirmDialog";
import { useStationCapabilities } from "@/hooks/useStationCapabilities";
import { tr } from "@/lib/i18n";
import {
  autodetectConfirmCopy,
  autodetectView,
  readAutodetectReason,
  readEffectiveAutodetect,
  readStoredAutodetect,
} from "@/lib/serialAutodetect";
import type { SerialAutodetect } from "@/lib/stationCapabilities";
import { switchBlocker } from "@/lib/stationProfile";
import { settingsWriteProblem, type SettingsPatch } from "@/lib/settingsWrite";

const PIN_INPUT =
  "w-full rounded-md border border-mast-border bg-mast-bg px-3 py-2 text-sm text-mast-text outline-none focus:border-mast-accent";

/** `error` from the typed client is the parsed response body, not an Error. */
function asError(e: unknown): Error {
  if (e instanceof Error) return e;
  const detail = (e as { detail?: unknown } | null)?.detail;
  return new Error(typeof detail === "string" && detail ? detail : String(e));
}

/** The one writer of `serial_autodetect`: the choice and the PIN typed into the confirmation. */
function useSerialAutodetectWrite() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ next, pin }: { next: boolean; pin: string }) => {
      // Not in the generated schema until it is regenerated.
      const body = { serial_autodetect: next, admin_pin: pin } as SettingsPatch;
      const { data, error } = await api.POST("/api/settings", { body });
      if (error) throw asError(error);
      const problem = settingsWriteProblem(data);
      if (problem) throw new Error(problem);
      return data;
    },
    onSettled: () =>
      Promise.all([
        qc.invalidateQueries({ queryKey: ["settings"] }),
        qc.invalidateQueries({ queryKey: ["health"] }),
        qc.invalidateQueries({ queryKey: ["environment", "sensors"] }),
      ]),
  });
}

export function SerialAutodetectSetting() {
  const station = useStationCapabilities();
  const mode = station.serialAutodetect;
  // STM ("default_on") or a service that does not say: not a pixel, not a request.
  if (mode !== "forced_off" && mode !== "default_off") return null;
  return <SerialAutodetectRow mode={mode} />;
}

function SerialAutodetectRow({ mode }: { mode: SerialAutodetect }) {
  const sensors = useQuery({
    queryKey: ["environment", "sensors"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/environment/sensors");
      if (error) throw error;
      return data;
    },
  });
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/settings");
      if (error) throw error;
      return data;
    },
  });
  const reason = readAutodetectReason(sensors.data);
  const view = autodetectView({
    mode,
    effective: readEffectiveAutodetect(sensors.data),
    stored: readStoredAutodetect(settings.data),
  });
  // The choice waiting for the confirmation; nothing is saved while it is set.
  const [asking, setAsking] = useState<boolean | null>(null);
  const write = useSerialAutodetectWrite();
  const err = write.error as Error | null;

  const confirm = (pin: string) => {
    if (asking === null) return;
    write.mutate({ next: asking, pin }, { onSuccess: () => setAsking(null) });
  };
  const cancel = () => {
    if (write.isPending) return;
    setAsking(null);
    write.reset();
  };

  return (
    <div
      className="rounded-mast-ctl border border-mast-border bg-mast-panel-2 px-3 py-2 text-xs text-mast-muted"
      data-testid="serial-autodetect"
      data-mode={mode}
    >
      <div className="flex flex-wrap items-center gap-3">
        <span className="font-medium text-mast-text" title={reason ?? undefined}>
          {view.status}
        </span>
        {view.switchable && (
          <div className={write.isPending || !settings.isSuccess ? "pointer-events-none opacity-50" : ""}>
            <Toggle
              checked={view.checked}
              onChange={(v) => {
                write.reset();
                setAsking(v);
              }}
              label={tr("打开串口自动探测（要管理 PIN）", "Serial auto-detection (admin PIN)")}
            />
          </div>
        )}
      </div>
      {reason && <p className="mt-1 font-mono text-[11px] text-mast-faint">{reason}</p>}
      {view.pendingNote && (
        <p className="mt-1 text-mast-warn" role="status">
          {view.pendingNote}
        </p>
      )}
      <SerialAutodetectDialog
        open={asking !== null}
        next={asking ?? false}
        busy={write.isPending}
        error={write.isError ? String(err?.message ?? err) : null}
        onConfirm={confirm}
        onClose={cancel}
      />
    </div>
  );
}

function SerialAutodetectDialog({
  open,
  next,
  busy,
  error,
  onConfirm,
  onClose,
}: {
  open: boolean;
  next: boolean;
  busy: boolean;
  error: string | null;
  onConfirm: (pin: string) => void;
  onClose: () => void;
}) {
  const [pin, setPin] = useState("");
  // Same key as the Advanced tab's PIN gate and the station-profile confirmation.
  const status = useQuery({
    queryKey: ["admin", "pin-status"],
    queryFn: async () => {
      const { data, error: e } = await api.GET("/api/admin/pin-status");
      if (e) throw e;
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

  const copy = autodetectConfirmCopy(next);
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
      onConfirm={() => {
        if (blocker === null) onConfirm(pin);
      }}
      onClose={onClose}
    >
      <p className="text-sm text-mast-text">{copy.body}</p>
      {pinIsSet === false ? (
        <div>
          <div
            role="alert"
            className="rounded border border-mast-warn/40 bg-mast-warn/10 px-3 py-2 text-sm text-mast-warn"
          >
            {tr(
              "这台机器还没有设置管理 PIN，这一项是受 PIN 保护的设置。可以就在下面设一个，也可以到",
              "No admin PIN is set on this machine, and this is a PIN-protected setting. Set one right below, or under ",
            )}
            <Link to="/settings/admin" className="mx-1 underline">
              {tr("设置 → 高级", "Settings → Advanced")}
            </Link>
            {tr("设置。设好之后再在这里输入一次。", ". Then type it here once more.")}
          </div>
          <SetPinForm onDone={() => setPin("")} />
        </div>
      ) : (
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
            {tr("PIN 只随这一次保存发送，不会被存下来。", "The PIN is sent once with this save and is not stored.")}
          </span>
        </label>
      )}
    </ConfirmDialog>
  );
}
