import { useQuery } from "@tanstack/react-query";
import { api } from "@/api/client";
import { SelectField, TextField } from "@/components/controls";
import {
  channelListNote,
  channelOptions,
  type SignalChannelOption,
} from "@/lib/signalChannels";
import { tr } from "@/lib/i18n";

// Signal channel picker: users choose a channel from a dropdown instead of typing
// a bare index such as 86.
//
// Why a bare number is a real problem, not just an inconvenience: index 86 on a real
// instrument is `LI Demod 1 X (A)`. Whoever types it must first look up whether 86
// is X or Y, and an instrument's `LI Demod` may have only X/Y, no R. A wrong index does
// not raise an error; dI/dV silently reads another component, and every plot and
// spectrum comes out wrong. The dropdown shows "index · channel name" together, so
// the mapping explains itself.
//
// When the list is unavailable, fall back to a free-text input, never an empty
// dropdown. An empty dropdown would make an already-entered value look blank, and
// the user's next step would be to retype it with a number they cannot look up.
//
// The decision logic (option assembly, the three degradation reasons) lives in
// lib/signalChannels.ts so it can be unit-tested: node --test cannot strip JSX from
// .tsx files, so the part that can go wrong is kept out of this file.

export function useSignalChannels() {
  return useQuery({
    // Shares the key with SignalCapturePanel on purpose: the two must not each
    // poll the instrument for a table that changes only when someone rewires
    // the rack. `staleTime: Infinity` matches api/tips.ts — the other
    // backend-served vocabulary in this app.
    queryKey: ["experimental", "signals"],
    staleTime: Infinity,
    gcTime: 30 * 60_000,
    retry: 1,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/experimental/signals");
      if (error) throw error;
      return data;
    },
  });
}

export function SignalIndexField({
  value,
  onCommit,
  /** Offer an "Auto (look up by channel name)" option mapped to this value. qPlus uses -1. */
  autoValue,
  placeholder,
}: {
  /** Stored value as text; "" = unset. */
  value: string;
  onCommit: (next: string) => void;
  autoValue?: number;
  placeholder?: string;
}) {
  const q = useSignalChannels();
  const channels = (q.data?.channels ?? []) as SignalChannelOption[];
  const note = channelListNote({
    pending: q.isPending,
    error: q.isError,
    degraded: q.data?.degraded,
    truncated: q.data?.truncated,
    declared_n: q.data?.declared_n,
    n_channels: q.data?.n_channels,
    count: channels.length,
  });

  if (note) {
    return (
      <div className="space-y-1">
        <TextField value={value} onChange={onCommit} placeholder={placeholder} mono />
        <p className="text-xs text-mast-warn">{note}</p>
      </div>
    );
  }

  return (
    <div className="space-y-1">
      <SelectField
        value={value}
        onChange={onCommit}
        options={channelOptions(channels, value, autoValue)}
      />
      <p className="text-xs text-mast-faint">
        {tr(`共 ${channels.length} 路。存下来的是索引，名字只用来认。`, `${channels.length} channels. The stored value is the index; the name is only for identification.`)}
      </p>
    </div>
  );
}
