// LiveHub `status` stream: lines of text with a level (acquisition start/end,
// manual takeover, dead-man switch, ...). Newest at the bottom; follows the
// scroll while at the bottom and stays put when the user scrolls up.

import { useEffect, useRef } from "react";
import { Badge, EmptyNote } from "@/components/ui";
import { useLiveStream, useLiveTick } from "@/hooks/useLive";
import { langTag, tr } from "@/lib/i18n";

const LEVEL_TONE: Record<string, string> = {
  debug: "default",
  info: "INFO",
  warn: "WARN",
  warning: "WARN",
  error: "DANGEROUS",
  critical: "DANGEROUS",
};

const LEVEL_LABEL: Record<string, string> = {
  debug: tr("调试", "Debug"),
  info: tr("信息", "Info"),
  warn: tr("注意", "Warning"),
  warning: tr("注意", "Warning"),
  error: tr("错误", "Error"),
  critical: tr("严重", "Critical"),
};

export function LiveStatusLog({ streamId, compact = false }: { streamId: string; compact?: boolean }) {
  const st = useLiveStream(streamId);
  useLiveTick(streamId, 4);
  const box = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);
  const lines = st?.buf.kind === "status" ? st.buf.lines : [];
  const shown = lines.slice(-(compact ? 50 : 300));
  const last = shown.at(-1)?.seq ?? 0;

  useEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [last]);

  if (!st) return <EmptyNote label={tr(`等待流 ${streamId} 的元数据…`, `Waiting for metadata of stream ${streamId}…`)} />;
  if (!shown.length) return <EmptyNote label={tr("还没有状态消息", "No status messages yet")} />;

  return (
    <div
      ref={box}
      onScroll={(e) => {
        const el = e.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
      }}
      className={`overflow-y-auto rounded-lg border border-mast-border bg-mast-bg p-2 font-mono text-xs ${compact ? "max-h-40" : "max-h-72"}`}
    >
      {shown.map((l) => {
        const lvl = l.level.toLowerCase();
        return (
          <div key={l.seq} className="flex items-start gap-2 py-0.5">
            <span className="shrink-0 tabular-nums text-mast-faint">
              {l.t !== null ? new Date(l.t * 1000).toLocaleTimeString(langTag(), { hour12: false }) : `#${l.seq}`}
            </span>
            <Badge tone={LEVEL_TONE[lvl] ?? "default"}>{LEVEL_LABEL[lvl] ?? l.level}</Badge>
            <span className="min-w-0 whitespace-pre-wrap break-words text-mast-text">{l.text}</span>
          </div>
        );
      })}
    </div>
  );
}
