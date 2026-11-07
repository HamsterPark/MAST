import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import { fmtCurrent } from "@/lib/units";
import { tr } from "@/lib/i18n";
import { CURVE_BOX, curveGeometry } from "@/lib/curvePath";

// Thumbnail curve for a current-monitor event — the waveform an alert was actually judged on.
//
// A `tip_quality_drop` from the current monitor has no scan frame near it: the
// event comes from a 1 kHz current segment. The segment itself is stored, though,
// and drawing it is the one picture that belongs next to this judgement. Borrowing
// a nearby scan image instead would misrepresent the history and must not be done.
//
// The segment id rides in `cause_ref` as `current_monitor#<seg_id>`, and that id
// is the same one /api/monitoring/segments/{seg_id}/data takes — both come from
// MonitoringService's `seg_id`.
//
// Sizing: an SVG with a viewBox scales to its container by itself, so this needs
// no ResizeObserver — that machinery (hooks/useElementWidth + lib/chartSize)
// exists for <canvas>, which cannot honour `width: 100%`. Nothing here is in px.

/** Enough shape to see a transient; far below the 4000 the detail view asks for. */
const THUMB_POINTS = 400;

/** The expanded alert row is a wide strip, so it can carry more of the trace. */
const WIDE_POINTS = 1200;

function Frame({
  children,
  title,
  wide,
}: {
  children: React.ReactNode;
  title: string;
  wide?: boolean;
}) {
  return (
    <div
      title={title}
      className={
        "flex w-full flex-col justify-center rounded border border-mast-border bg-mast-bg p-1.5 " +
        (wide ? "aspect-[4/1]" : "aspect-square")
      }
    >
      {children}
    </div>
  );
}

function Caption({ children }: { children: React.ReactNode }) {
  return <div className="mt-1 truncate text-center text-[10px] text-mast-muted">{children}</div>;
}

export function SegmentCurveThumb({
  segId,
  hint,
  wide,
}: {
  segId: number;
  hint: string;
  /** Wide strip for the expanded alert row, instead of the square tile. */
  wide?: boolean;
}) {
  const maxPoints = wide ? WIDE_POINTS : THUMB_POINTS;
  const q = useQuery({
    // A written segment is immutable; the only thing that changes is whether the
    // retention sweep has taken the .npy, and that is slow. Same staleTime the
    // detail pane uses.
    staleTime: Infinity,
    queryKey: ["monitoring", "segment-data", segId, maxPoints],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/monitoring/segments/{seg_id}/data", {
        params: { path: { seg_id: segId }, query: { max_points: maxPoints } },
      });
      if (error) throw error;
      return data;
    },
  });

  if (q.isPending) {
    return (
      <Frame title={hint} wide={wide}>
        <div className="text-center text-[10px] text-mast-faint">
          {tr(`载入第 ${segId} 段波形…`, `Loading segment ${segId} waveform…`)}
        </div>
      </Frame>
    );
  }

  const d = q.data;
  if (q.isError || !d?.ok) {
    // Say which of the two it is: "waveform unavailable" over a swept segment and
    // over a broken backend read the same, and only one of them is worth chasing.
    return (
      <Frame title={hint} wide={wide}>
        <div className="px-1 text-center text-[10px] leading-snug text-mast-muted">
          {tr(`电流监控 · 第 ${segId} 段`, `Current monitor · segment ${segId}`)}
          <div className="mt-0.5 text-mast-faint">
            {q.isError
              ? tr("波形读取失败", "Failed to read the waveform")
              : d?.detail || tr("该段波形已不可用", "The waveform for this segment is no longer available")}
          </div>
        </div>
      </Frame>
    );
  }

  const geo = curveGeometry(d.i_a ?? []);
  const envelope = d.source === "envelope";

  if (!geo.points) {
    return (
      <Frame title={hint} wide={wide}>
        <div className="text-center text-[10px] text-mast-muted">
          {tr(
            `第 ${segId} 段只有 ${geo.n} 个采样点，画不出曲线`,
            `Segment ${segId} has only ${geo.n} sample point(s); cannot draw a curve`,
          )}
        </div>
      </Frame>
    );
  }

  return (
    <Link
      // Point at the specific sub-page, not the group root: `/monitoring` is only a
      // redirect to the last-visited sub-page, which may be environment history and
      // would receive `?seg=` on a page that ignores it.
      to={`/monitoring/current?seg=${segId}`}
      title={`${hint}\n${tr(`点击打开第 ${segId} 段的完整波形与频谱`, `Click to open the full waveform and spectrum of segment ${segId}`)}`}
      className="block"
    >
      <Frame title="" wide={wide}>
        <svg
          viewBox={`0 0 ${CURVE_BOX} ${CURVE_BOX}`}
          preserveAspectRatio="none"
          className="h-full w-full"
          role="img"
          aria-label={tr(`第 ${segId} 段电流波形`, `Current waveform of segment ${segId}`)}
        >
          <polyline
            points={geo.points}
            fill="none"
            stroke="var(--mast-danger)"
            strokeWidth={1.2}
            vectorEffect="non-scaling-stroke"
            strokeLinejoin="round"
          />
        </svg>
      </Frame>
      <Caption>
        {tr(`第 ${segId} 段`, `Segment ${segId}`)} · {fmtCurrent(geo.min)} ～ {fmtCurrent(geo.max)}
        {envelope ? tr(" · 包络", " · envelope") : ""}
      </Caption>
    </Link>
  );
}
