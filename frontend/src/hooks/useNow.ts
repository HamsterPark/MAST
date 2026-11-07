import { useEffect, useState } from "react";

/** `Date.now()`, refreshed every `everyMs` so "waiting 3 min" and "expires in 7 h" stay true on screen. */
export function useNow(everyMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), everyMs);
    return () => clearInterval(t);
  }, [everyMs]);
  return now;
}
