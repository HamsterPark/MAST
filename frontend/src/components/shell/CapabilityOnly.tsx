import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { Badge, Card } from "@/components/ui";
import { useStationCapabilities } from "@/hooks/useStationCapabilities";
import { capabilityLabel, type Capability } from "@/lib/stationCapabilities";
import { profileLabel } from "@/lib/stationProfile";
import { tr } from "@/lib/i18n";

// A page that belongs to one kind of station (the nav's `requires`, lib/nav.ts).
//
// The nav already leaves it out on a station without the capability; this covers the other
// ways in: a bookmark, a link in a log, an old tab. Mounting the page there would start its
// polls against an instrument the station does not have (the signal monitor asks the
// Nanonis current monitor every second), so the page is not mounted at all, and the notice
// says which station it belongs to and where the station type is chosen.
//
// Nothing is mounted until the station has said what it has: an optical or a generic
// station must not send the page's first request in the instant before /api/health
// answers. A health check that fails counts as the default STM station, as everywhere.

export function CapabilityNotice({ cap }: { cap: Capability }) {
  const station = useStationCapabilities();
  return (
    <Card className="mx-auto mt-6 max-w-[640px] p-5">
      <div className="flex items-center gap-2">
        <Badge tone="INFO">{profileLabel(station.profile)}</Badge>
        <span className="text-[13px] font-semibold text-mast-text">
          {tr("这台机器上没有这一页", "This page does not apply to this machine")}
        </span>
      </div>
      <p className="mt-2 text-[12.5px] text-mast-muted">
        {tr(
          `这一页需要「${capabilityLabel(cap)}」。这台机器是「${profileLabel(station.profile)}」，没有它，所以这一页不打开，也不向仪器发任何请求。站点类型选错了的话，到「设置 → 常规设置 → 站点画像」改。`,
          `This page needs ${capabilityLabel(cap)}. This machine runs as "${profileLabel(station.profile)}", which does not have it, so the page is not opened and sends nothing to any instrument. If the station type is wrong, change it under Settings → General → Station profile.`,
        )}
      </p>
      <Link
        to="/settings/general#station-profile"
        className="mt-2 inline-block text-[12.5px] text-mast-accent underline"
      >
        {tr("前往站点画像 →", "Open the station profile →")}
      </Link>
    </Card>
  );
}

/** Render children only on a station with `cap`; otherwise explain why not. */
export function CapabilityOnly({ cap, children }: { cap: Capability; children: ReactNode }) {
  const station = useStationCapabilities();
  if (!station.ready) return null;
  return station.has(cap) ? <>{children}</> : <CapabilityNotice cap={cap} />;
}
