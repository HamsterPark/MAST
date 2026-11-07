// GET /api/settings/hardware-modules —— 选装硬件模块的开关状态（只读）。
//
// 查询键与设置页、能力开关里的同名 hook 一致，几处共用一份缓存。模块关着只是把它的技能从
// agent 的工具表里拿掉：技能仍在注册表里，页面上的按钮（/api/skills/{name}/execute）照常可用。

import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

export function useHardwareModules() {
  return useQuery({
    queryKey: ["settings", "hardware-modules"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/settings/hardware-modules");
      if (error) throw error;
      return data;
    },
    staleTime: 30_000,
  });
}
