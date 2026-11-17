// Environment-tag settings (docs/v2/design/data_env_tags.md D10) and the recorder's counters.
//
// They have their own endpoint and their own file in the gallery state directory, apart from the
// gallery config: saving the data roots rewrites config.json, and must never reset these.

import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { components } from "@/api/schema";

export type GalleryEnvTagsConfig = components["schemas"]["GalleryEnvTagsConfig"];

export const GALLERY_ENV_TAGS_KEY = ["gallery", "env-tags"] as const;

export function useGalleryEnvTags() {
  return useQuery({
    queryKey: GALLERY_ENV_TAGS_KEY,
    queryFn: async (): Promise<GalleryEnvTagsConfig> => {
      const { data, error } = await api.GET("/api/gallery/env-tags");
      if (error) throw error;
      return data;
    },
  });
}
