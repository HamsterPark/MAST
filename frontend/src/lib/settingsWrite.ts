/**
 * Whether a `POST /api/settings` response counts as "saved" — one criterion, one place.
 *
 * ── Why this file exists ──
 * A save action once turned out to be a complete no-op (the fields were missing
 * from the write schema and pydantic's default `extra='ignore'` dropped them all)
 * while the button still toasted a green "Saved". The direct cause was a missing
 * backend field, but it went unnoticed because the onSuccess handler only checked
 * `degraded`. This endpoint rejects with `ok:false, degraded:false` (port out of
 * range, malformed scan_policy/zctrl_presets, wrong admin PIN), so a rejection
 * and a success looked identical on the frontend.
 *
 * Six screens POST to this endpoint, and each remembering which fields to check
 * is exactly the per-page wiring that goes stale. So the criterion lives here:
 * after a POST, ask `settingsWriteProblem(data)` — non-null means nothing was
 * saved, and the string is the reason to show the operator.
 * `test/settingsWrite.test.ts` has a gate that requires every caller of this
 * endpoint to use it.
 *
 * It deliberately does NOT toast: some callers must first roll back an
 * optimistic update, others must keep what the user just typed. This only
 * answers "did it succeed".
 */

import { tr } from "./i18n.ts";

import type { components } from "@/api/schema";

/**
 * Request body of `POST /api/settings` — **generated from the backend schema, not
 * hand-copied**.
 *
 * The backend's `SettingsWriteRequest` is `extra='forbid'`: a key that is not on
 * the model is no longer silently dropped (which would return 200 + `ok:true`
 * with nothing written) but rejected with 422. The benefit is that a write that
 * did not land never reports success; the cost is that **sending one extra key
 * from the frontend fails the whole request with 422**.
 *
 * Callers therefore use this type rather than `Record<string, unknown>`: a
 * misspelled key, or a field removed on the backend, turns `npm run typecheck`
 * red immediately instead of failing at runtime.
 */
export type SettingsPatch = Partial<components["schemas"]["SettingsWriteRequest"]>;

export type SettingsWriteResult = {
  ok?: boolean;
  degraded?: boolean;
  /** Key → reason. Non-empty = the whole write was rejected; nothing was written. */
  rejected?: Record<string, string> | null;
  /** Keys guarded by the PIN (hardware_modules / advanced_capabilities / coarse_drive / station_profile…). */
  pin_required?: boolean;
  /** Human-readable message from the backend on PIN failure; also used for capability-switch rebuild notes. */
  rebuild_note?: string | null;
};

/**
 * Returns a reason string when the write did not land; null when it was really saved.
 *
 * The order is deliberate: PIN first (the backend puts the reason in
 * rebuild_note), then degraded (the kernel is not attached, so the write never
 * reached the store), then rejected (malformed structure/values, nothing
 * written), and finally the catch-all `ok !== true`. Dropping any layer would let
 * one class of failure silently turn into "saved".
 */
export function settingsWriteProblem(
  res: SettingsWriteResult | null | undefined,
): string | null {
  if (!res) return tr("保存失败：没有收到内核的回应。", "Save failed: no response from the kernel.");
  if (res.pin_required) return res.rebuild_note || tr("管理 PIN 不正确，未保存。", "Admin PIN is incorrect; not saved.");
  if (res.degraded) return tr("写入未生效（内核未接入）。", "The write did not take effect (kernel not attached).");
  const reasons = Object.values(res.rejected ?? {}).filter(Boolean);
  if (reasons.length > 0) return reasons.join(tr("；", "; "));
  if (res.ok !== true) return tr("保存被拒绝（内核未说明原因）。", "Save rejected (the kernel gave no reason).");
  return null;
}
