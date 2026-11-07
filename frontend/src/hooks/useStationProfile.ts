import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { tr } from "@/lib/i18n";
import { settingsWriteProblem, type SettingsPatch } from "@/lib/settingsWrite";
import {
  normalizeProfile,
  normalizeSource,
  restartNeeded,
  type ProfileSource,
  type StationProfile,
} from "@/lib/stationProfile";

// Which kind of station this MAST service is: "stm" (a Nanonis STM) or "optical"
// (an optical/Raman station, no STM). Two values are involved and they are not the
// same thing:
//   * the profile in force  - GET /api/health `station_profile` (what the UI should
//     look like), plus `station_restart_pending`: the stored value was changed
//     after the service started;
//   * the stored profile    - GET /api/settings `station_profile` (what the
//     operator chose; unset = "stm").
// The stored value is written through POST /api/settings and takes full effect on
// the next service start (the Nanonis connection and its threads are decided at
// start-up). The key is in the backend's admin-PIN list (`admin_pin.GUARDED_KEYS`),
// so every write carries the PIN the operator typed into the confirmation dialog;
// the PIN is a variable of the mutation and goes nowhere else.
//
// Anything unreadable is "stm", i.e. the behaviour every machine had before
// profiles existed.

// Both keys are shared with other screens (DashboardPage, TopBar, RightPanel read
// ["health"]; SettingsPage and useLegacyFeatures read ["settings"]), so one
// request serves them all.
const HEALTH_KEY = ["health"] as const;
const SETTINGS_KEY = ["settings"] as const;
const WRITE_TIMEOUT_MS = 8000;

/** `error` from the typed client is the parsed response body, not an Error. */
function asError(e: unknown): Error {
  if (e instanceof Error) return e;
  const detail = (e as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail) return new Error(detail);
  try {
    return new Error(JSON.stringify(e));
  } catch {
    return new Error(String(e));
  }
}

/** One switch of the profile: the choice and the admin PIN that authorises saving it. */
export type SwitchRequest = { profile: StationProfile; pin: string };

export function useStationProfile() {
  const qc = useQueryClient();

  const health = useQuery({
    queryKey: HEALTH_KEY,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/health");
      if (error) throw error;
      return data;
    },
  });
  const settings = useQuery({
    queryKey: SETTINGS_KEY,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/settings");
      if (error) throw error;
      return data;
    },
  });

  // `data` survives a failed refetch, so one dropped poll does not flip the UI
  // between profiles. Before the first answer (or if health never answers) the
  // profile is the default.
  const loaded = health.data !== undefined && settings.data !== undefined;
  const profile: StationProfile = normalizeProfile(health.data?.station_profile);
  const configured: StationProfile = normalizeProfile(settings.data?.station_profile);
  const source: ProfileSource = normalizeSource(health.data?.station_profile_source);

  // The backend flag is the authority: it compares the stored value with the profile
  // the process started under. Comparing stored vs. in-force here is only the
  // fallback for a backend that does not send the flag, because when an
  // environment variable pins the profile the two legitimately differ and no
  // restart would change that.
  const flag = health.data?.station_restart_pending;
  const restartPending =
    typeof flag === "boolean" ? flag : loaded && restartNeeded(configured, profile);

  const setProfile = useMutation({
    mutationFn: async ({ profile: next, pin }: SwitchRequest) => {
      const ctrl = new AbortController();
      const timer = window.setTimeout(() => ctrl.abort(), WRITE_TIMEOUT_MS);
      try {
        const body: SettingsPatch = { station_profile: next, admin_pin: pin };
        const { data, error } = await api.POST("/api/settings", {
          body,
          signal: ctrl.signal,
        });
        if (error) throw asError(error);
        const problem = settingsWriteProblem(data);
        if (problem) throw new Error(problem);
        return data;
      } catch (e) {
        if (ctrl.signal.aborted) {
          throw new Error(
            tr(
              "保存超时：8 秒内没有收到内核的回应。",
              "Save timed out: no reply from the kernel within 8 s.",
            ),
          );
        }
        throw e;
      } finally {
        window.clearTimeout(timer);
      }
    },
    // Refresh both: the stored value and the restart flag move together. Returning
    // the promise keeps the mutation pending until they are back, so "saved" is
    // never shown next to a restart flag that has not caught up yet.
    onSettled: () =>
      Promise.all([
        qc.invalidateQueries({ queryKey: SETTINGS_KEY }),
        qc.invalidateQueries({ queryKey: HEALTH_KEY }),
      ]),
  });

  return {
    /** The profile in force (GET /api/health); "stm" until health answers. */
    profile,
    isOptical: profile === "optical",
    /** The stored choice (GET /api/settings); "stm" when never set. */
    configured,
    /** Where the configured profile comes from; "env" = pinned by MAST_STATION_PROFILE. */
    source,
    /** Saved, but the running service has not restarted into it yet. */
    restartPending,
    /** Health and settings have both answered at least once. */
    loaded,
    setProfile,
  };
}
