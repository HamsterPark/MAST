import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { settingsWriteProblem, type SettingsPatch } from "@/lib/settingsWrite";

// Legacy features (built-in agent chat, Agents, query assistant, multi-day conduct,
// memory, vision model, conversation feedback, wishlist feedback) are hidden unless
// the backend setting `legacy_features_enabled` is on. The value lives on the
// backend so every browser of the same MAST service sees the same UI, and so the
// backend can skip loading the vision model at start-up.

const SETTINGS_KEY = ["settings"] as const;
const WRITE_TIMEOUT_MS = 8000;

export function useLegacyFeatures() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: SETTINGS_KEY,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/settings");
      if (error) throw error;
      return data;
    },
  });
  const enabled =
    (q.data as { legacy_features_enabled?: boolean | null } | undefined)
      ?.legacy_features_enabled === true;

  const setEnabled = useMutation({
    mutationFn: async (on: boolean) => {
      const ctrl = new AbortController();
      const timer = window.setTimeout(() => ctrl.abort(), WRITE_TIMEOUT_MS);
      try {
        const body = { legacy_features_enabled: on } as SettingsPatch;
        const { data, error } = await api.POST("/api/settings", {
          body,
          signal: ctrl.signal,
        });
        if (error) throw error;
        const problem = settingsWriteProblem(data);
        if (problem) throw new Error(problem);
        return data;
      } finally {
        window.clearTimeout(timer);
      }
    },
    onSettled: () => qc.invalidateQueries({ queryKey: SETTINGS_KEY }),
  });

  return { enabled, loaded: q.isSuccess, setEnabled };
}
