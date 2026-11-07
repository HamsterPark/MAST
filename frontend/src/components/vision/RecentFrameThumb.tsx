import { useState } from "react";
import { tr } from "@/lib/i18n";
import { framePlaceholder, type FrameLike } from "@/lib/visionFrames";
import { SegmentCurveThumb } from "@/components/monitoring/SegmentCurveThumb";
import { Modal } from "@/components/controls";

// The thumbnail slot of a "recent annotated frame" tile.
//
// Shared by ChatPage and VisionPage so the rendered text, the current-monitor
// curve and the click-to-enlarge lightbox live in one place; putting any of them
// in the pages would leave the other page with the old behaviour.
//
// ── click to enlarge ──────────────────────────────────────────────────────
// A picture tile is a button that opens a lightbox. The current-monitor tiles
// are different: SegmentCurveThumb is a <Link> to the full waveform.
//
// The enlarged picture comes from `/api/vision/recent-frame/{seqno}` and NOT
// from scaling up `image_b64`: that thumbnail is 160 px, so stretching it in the
// browser would produce the same 160 px with bigger squares.
//
// The modal opens ONLY on click. It is never opened by an effect: an auto-opened
// full-screen overlay hides the live readings during manual intervention, and a
// picture is never urgent enough for that. `test/recentFrameZoom.test.ts` holds
// that line.

function FrameLightbox({
  frame,
  onClose,
}: {
  frame: FrameLike;
  onClose: () => void;
}) {
  const [failed, setFailed] = useState(false);
  const seqno = frame.seqno;
  const name = frame.file_path?.split(/[\\/]/).pop();
  return (
    <Modal open onClose={onClose} wide title={`${tr("标注帧", "Annotated frame")} #${seqno} · ${frame.kind || tr("事件", "event")}`}>
      {failed ? (
        <div className="py-10 text-center text-sm text-mast-muted">
          {tr("这一帧的图取不回来了。", "The image for this frame could not be loaded.")}
          <div className="mt-1 text-xs text-mast-faint">
            {tr(
              "事件还在，图不在 —— 文件可能已被移动或删除。缩略图是之前渲染并缓存下来的。",
              "The event is still recorded but the image is gone — the file may have been moved or deleted. The thumbnail was rendered and cached earlier.",
            )}
          </div>
        </div>
      ) : (
        <img
          src={`/api/vision/recent-frame/${encodeURIComponent(String(seqno))}`}
          alt={
            frame.kind
              ? `${tr("标注帧", "Annotated frame")} #${seqno} (${frame.kind})`
              : `${tr("标注帧", "Annotated frame")} #${seqno}`
          }
          onError={() => setFailed(true)}
          /* object-contain, not cover: the tile crops to a square grid cell, and
             seeing the whole frame is the entire point of opening this. */
          className="mx-auto max-h-[72vh] w-auto max-w-full object-contain"
        />
      )}
      {name && (
        <div className="mt-3 truncate text-center font-mono text-xs text-mast-muted" title={frame.file_path ?? ""}>
          {name}
        </div>
      )}
    </Modal>
  );
}

export function RecentFrameThumb({ frame, alt }: { frame: FrameLike; alt?: string }) {
  const [zoom, setZoom] = useState(false);
  const placeholder = framePlaceholder(frame);

  if (!placeholder) {
    // A tile whose seqno never arrived cannot be asked for by the endpoint, so
    // it stays a plain picture rather than a button that opens an empty box.
    const canZoom = frame.seqno != null;
    const img = (
      <img
        src={`data:image/png;base64,${frame.image_b64}`}
        alt={alt || frame.kind || tr("标注帧", "Annotated frame")}
        className="aspect-square w-full rounded border border-mast-border object-cover"
      />
    );
    if (!canZoom) return img;
    return (
      <>
        <button
          type="button"
          onClick={() => setZoom(true)}
          title={tr("点击放大", "Click to enlarge")}
          className="block w-full rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-mast-accent"
        >
          {img}
        </button>
        {zoom && <FrameLightbox frame={frame} onClose={() => setZoom(false)} />}
      </>
    );
  }

  // No image, but this event points at data of its OWN. Draw that — never
  // something borrowed from a neighbouring event.
  if (placeholder.source?.kind === "current_monitor") {
    return <SegmentCurveThumb segId={placeholder.source.segmentId} hint={placeholder.hint} />;
  }

  return (
    <div
      title={placeholder.hint}
      className="flex aspect-square w-full items-center justify-center rounded border border-dashed border-mast-border px-2 text-center text-xs leading-snug text-mast-muted"
    >
      {placeholder.label}
    </div>
  );
}
