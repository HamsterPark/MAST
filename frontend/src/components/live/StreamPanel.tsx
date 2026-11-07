// Fixed panel on a page: live spectrum, the TERS map being built, PMT, pump-probe, ...
// A title, a stream-selection dropdown (default "Follow latest") and below it the
// default renderer for that stream.
//
// "Follow latest" is followLatest in lib/live/pick.ts: the stream whose name fits
// the purpose, that is running and receiving data, and it is sticky: when a run
// ends and a new run declares its stream the panel switches over, but it does not
// flip back and forth between two streams that are both receiving data. It
// re-evaluates immediately when the list changes, when a candidate gets a new
// frame, and every FOLLOW_EVAL_MS (freshness expires); an unchanged result does
// not re-render. The displayed stream is subscribed naturally; `prefer` candidates
// are subscribed too, other candidates only show whether data is flowing if the
// page subscribes to them elsewhere.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Card, EmptyNote } from "@/components/ui";
import { SelectField } from "@/components/controls";
import { useLiveStreams } from "@/hooks/useLive";
import { getLiveStore, watchStream } from "@/lib/liveClient";
import { candidates, followLatest, matching } from "@/lib/live/pick";
import { LiveStreamBody, LiveStreamHeader } from "./LiveStreamView";
import { tr } from "@/lib/i18n";

const AUTO = "__auto__";
/** Freshness expires over time: re-evaluate periodically even without new frames. */
const FOLLOW_EVAL_MS = 2_000;
/** Delay before re-evaluating after a candidate gets a new frame (even a 20 Hz spectrum is evaluated no more often than this). */
const FOLLOW_KICK_MS = 300;

export function StreamPanel({
  title,
  kind,
  hint,
  prefer,
  strict = false,
  emptyLabel,
  actions,
  compact = false,
  onShown,
}: {
  title: string;
  /** One or several kinds (pump-probe: single-instrument xy, dual-instrument map2d). */
  kind: string | readonly string[];
  /** Name preference (see STREAM_HINTS). */
  hint?: RegExp;
  /** When following the latest, these streams are shown first while they are receiving data (live spectrum: the averaged spectra acquired by MAST take priority over the satellite's per-frame stream). */
  prefer?: RegExp;
  /** true: list only name-matching streams (the optics page splits panels by purpose); false: all streams of the kind are selectable, matching ones first. */
  strict?: boolean;
  emptyLabel: string;
  actions?: ReactNode;
  compact?: boolean;
  /** Which stream is being shown (the page uses it to collapse that stream in the stream list by default). */
  onShown?: (id: string | null) => void;
}) {
  const all = useLiveStreams();
  const cands = strict && hint ? matching(all, kind, hint) : candidates(all, kind, hint);
  const [chosen, setChosen] = useState<string | null>(null);
  const [followed, setFollowed] = useState<string | null>(null);

  const auto = followLatest(cands, followed, Date.now(), { hint, prefer });
  useEffect(() => {
    if (auto !== followed) setFollowed(auto);
  }, [auto, followed]);

  // Subscribe to `prefer` candidates too, so it is visible when they are receiving data (an averaged spectrum is one frame per acquisition, so subscribing costs almost no traffic)
  const preferKey = prefer
    ? cands
        .filter((s) => prefer.test(s.meta.id) && !s.meta.ended && !s.gone)
        .map((s) => s.meta.id)
        .join("\n")
    : "";
  useEffect(() => {
    if (!preferKey) return;
    const offs = preferKey.split("\n").map((sid) => watchStream(sid));
    return () => offs.forEach((off) => off());
  }, [preferKey]);

  const latest = useRef({ cands, hint, prefer });
  latest.current = { cands, hint, prefer };
  useEffect(() => {
    let kick: ReturnType<typeof setTimeout> | undefined;
    const evaluate = () => {
      kick = undefined;
      const { cands: c, hint: h, prefer: p } = latest.current;
      setFollowed((prev) => followLatest(c, prev, Date.now(), { hint: h, prefer: p }));
    };
    const timer = setInterval(evaluate, FOLLOW_EVAL_MS);
    // Re-evaluate soon after a candidate gets a new frame (switch as soon as an averaged spectrum arrives), at most once per FOLLOW_KICK_MS
    const off = getLiveStore().onChange((sid) => {
      if (kick === undefined && latest.current.cands.some((s) => s.meta.id === sid)) {
        kick = setTimeout(evaluate, FOLLOW_KICK_MS);
      }
    });
    return () => {
      clearInterval(timer);
      off();
      if (kick !== undefined) clearTimeout(kick);
    };
  }, []);

  const pinned = chosen !== null && cands.some((s) => s.meta.id === chosen);
  const id = pinned ? chosen : auto;
  const st = id ? cands.find((s) => s.meta.id === id) : undefined;
  const autoTitle = auto ? cands.find((s) => s.meta.id === auto)?.meta.title : undefined;

  useEffect(() => {
    onShown?.(id);
  }, [id, onShown]);

  return (
    <Card className="min-w-0">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h3 className="mr-auto text-sm font-semibold text-mast-text">{title}</h3>
        <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">
          {cands.length > 1 && (
            <div className="min-w-0 max-w-full sm:w-80 [&_select]:w-full">
              <SelectField
                value={pinned ? chosen : AUTO}
                onChange={(v) => setChosen(v === AUTO ? null : v)}
                options={[
                  { value: AUTO, label: `${tr("跟随最新", "Follow latest")}${autoTitle ? ` (${autoTitle})` : ""}` },
                  ...cands.map((s) => ({
                    value: s.meta.id,
                    label: `${s.meta.title} · ${s.meta.id}${s.meta.ended ? tr("（已结束）", " (ended)") : ""}`,
                  })),
                ]}
              />
            </div>
          )}
          {actions}
        </div>
      </div>
      {st ? (
        <div className="space-y-3">
          <LiveStreamHeader st={st} />
          {/* key: rebuild the whole view when the stream changes; pause point, channel selection and offscreen canvas belong to the previous stream */}
          <LiveStreamBody key={st.meta.id} st={st} compact={compact} />
        </div>
      ) : (
        <EmptyNote label={emptyLabel} />
      )}
    </Card>
  );
}
