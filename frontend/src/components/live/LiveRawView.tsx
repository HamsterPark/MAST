// Fallback for a stream kind the frontend does not know: show the raw text of the
// most recent frames instead of nothing. A newly declared kind is at least visible
// here (data flowing, its shape) before a dedicated renderer is written.

import { EmptyNote } from "@/components/ui";
import { useLiveStream, useLiveTick } from "@/hooks/useLive";
import { tr } from "@/lib/i18n";

export function LiveRawView({ streamId }: { streamId: string }) {
  const st = useLiveStream(streamId);
  useLiveTick(streamId, 2);
  if (!st) return <EmptyNote label={tr(`等待流 ${streamId} 的元数据…`, `Waiting for metadata of stream ${streamId}…`)} />;
  const frames = st.buf.kind === "raw" ? st.buf.frames.slice(-3).reverse() : [];
  return (
    <div className="space-y-1">
      <p className="text-xs text-mast-muted">
        {tr(`这种 kind（${st.meta.kind}）前端还没有专门的画法，下面是最近几帧的原文。`, `The frontend has no dedicated renderer for this kind (${st.meta.kind}); the raw text of the most recent frames is shown below.`)}
      </p>
      {frames.length ? (
        frames.map((f) => (
          <pre
            key={f.seq}
            className="max-h-40 overflow-auto rounded border border-mast-border bg-mast-bg p-2 text-[11px] text-mast-muted"
          >
            #{f.seq} {truncate(JSON.stringify(f.frame, null, 1))}
          </pre>
        ))
      ) : (
        <EmptyNote label={tr("还没有帧", "No frames yet")} />
      )}
    </div>
  );
}

function truncate(s: string | undefined): string {
  if (!s) return "";
  return s.length > 4000 ? tr(`${s.slice(0, 4000)} …（共 ${s.length} 字符）`, `${s.slice(0, 4000)} … (${s.length} characters total)`) : s;
}
