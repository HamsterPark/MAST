import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import { CONDUCT_SETTINGS_TITLE, directorStateNote, badgeTone } from "@/lib/conduct";
import type { SettingsPatch } from "@/lib/settingsWrite";
import { KnobCatalogSection } from "./KnobCatalogSection";
import { getLang, tr } from "@/lib/i18n";

// Settings -> conduct director thread.
//
// This section is the UI entry point for the conduct knobs; the backend read,
// write and type paths already exist, and ConductPage links here via
// CONDUCT_SETTINGS_TITLE.
//
// Two knobs deserve a note in the blurb:
//   - cd_enabled is OFF by default. While off, the thread and its tables are
//     never created, so behaviour is byte-for-byte what it was before the
//     feature existed. It is not a "pause".
//   - cd_autonomy has three levels that decide WHO may approve, not WHAT may be
//     done. The safety envelope is identical at every level; only the
//     "an operator is present" requirement is relaxed.

export function ConductSettingsSection({
  settings,
  saving,
  save,
}: {
  settings: { data?: Record<string, unknown> | null };
  saving: boolean;
  save: (patch: SettingsPatch) => void;
}) {
  // Same queryKey as ConductPage: saving on either page invalidates the other,
  // so neither keeps a stale `enabled`.
  const config = useQuery({
    queryKey: ["conduct", "config"],
    staleTime: 5_000,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/conducts/config");
      if (error) throw error;
      return data as { enabled?: boolean; director_running?: boolean };
    },
  });

  const note = directorStateNote(
    Boolean(config.data?.enabled),
    Boolean(config.data?.director_running),
  );

  return (
    <>
      {config.data && (
        <p className={"mb-3 rounded-mast-ctl px-3 py-2 text-sm " + badgeTone(note.tone)}>
          {note.text}
        </p>
      )}
      <KnobCatalogSection
        configPath="/api/conducts/config"
        queryKey={["conduct", "config"]}
        settingsKey="conduct"
        settings={settings}
        saving={saving}
        save={save}
        unavailableNote={tr(
          "conduct 模块未装载，读不到旋钮目录，因此不提供编辑入口——宁可什么都不显示，也不给一份改了不生效的表单。",
          "The conduct module is not loaded, so the knob catalogue cannot be read and no editor is offered. Showing nothing is better than a form whose changes have no effect.",
        )}
        blurb={
          getLang() === "zh" ? (
            <>
              多天执行编排（hour~day）的常驻线程：声明式阶段状态机 + 闸门 + 断点续跑。
              <b>出厂默认关</b>——关着的时候线程不建、表不建，逐字节等于这个功能落地之前；
              <b>关掉它不会关掉读端点</b>，已有 conduct 的状态照常读得到、中止照常按得下。
              「自主度」三档管的是<b>谁能点这个头</b>，不是<b>什么可以做</b>：
              三档下安全包络一个数都不放宽，放开的只是「夜里三点没有人在场」那道在场约束。
            </>
          ) : (
            <>
              A resident thread for multi-day (hours to days) orchestration: a declarative
              stage state machine with gates and resume-after-restart.
              <b> Off by default</b>: while off, the thread and its tables are never created,
              so behaviour is identical to before the feature existed.
              <b> Turning it off does not disable the read endpoints</b>: the state of existing
              conducts stays readable and abort still works.
              The three autonomy levels decide <b>who may approve</b>, not <b>what may be done</b>:
              the safety envelope is not relaxed at any level; only the requirement that
              someone is present at 3 a.m. is.
            </>
          )
        }
      />
    </>
  );
}

export { CONDUCT_SETTINGS_TITLE };
