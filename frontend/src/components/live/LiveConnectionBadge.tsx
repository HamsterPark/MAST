// State of the live channel, so it is obvious whether the curves on screen are
// still updating. A small green badge while push works; polling fallback,
// reconnecting and a missing backend endpoint are all reported (a frozen chart
// that does not say so is worse than no chart).

import { Badge } from "@/components/ui";
import { fmtAgo, useLiveConnection, useNow } from "@/hooks/useLive";
import { tr } from "@/lib/i18n";

export function LiveConnectionBadge() {
  const c = useLiveConnection();
  const now = useNow(2000);
  let tone = "default";
  let text = "";
  switch (c.status) {
    case "open":
      tone = "AUTO";
      text = tr("实时推送", "Live push");
      break;
    case "connecting":
      tone = "INFO";
      text = tr("连接中…", "Connecting…");
      break;
    case "reconnecting":
      tone = "WARN";
      text = c.polling ? tr("重连中 · 轮询兜底", "Reconnecting · polling fallback") : tr("重连中…", "Reconnecting…");
      break;
    case "failed":
      tone = "WARN";
      text = c.polling ? tr("推送不可用 · 轮询中", "Push unavailable · polling") : tr("推送不可用", "Push unavailable");
      break;
    case "unsupported":
      tone = "INFO";
      text = tr("轮询（浏览器无 WebSocket）", "Polling (browser has no WebSocket)");
      break;
    default:
      return null;
  }
  return (
    <span className="inline-flex flex-wrap items-center gap-2 text-xs">
      <Badge tone={tone}>{text}</Badge>
      {c.lastTrafficMs !== null && <span className="text-mast-faint">{tr("最后收到", "Last received")} {fmtAgo(c.lastTrafficMs, now)}</span>}
      {c.problem && <span className="text-mast-warn">{c.problem}</span>}
    </span>
  );
}
