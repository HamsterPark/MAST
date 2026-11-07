// ════════════════════════════════════════════════════════════════════════════
// envPanel: pure decisions about which rows the right-hand ENVIRONMENT panel draws.
//
// Same relationship to `components/shell/RightPanel.tsx` as `lib/transcriptRefresh.ts`
// has to its hook: no React import, so every decision runs under node --test.
//
// Extracted because these decisions are all about when NOT to draw. A missed case shows
// up as one extra, normal-looking reading row: no crash, nothing for typecheck to catch.
// ════════════════════════════════════════════════════════════════════════════

/** The fields we use from `sensors[]` of `/api/environment/readings`. */
export interface EnvSensor {
  name: string;
  type?: string | null;
  kind?: string | null;
  placeholder?: boolean | null;
  value?: number | null;
  unit?: string | null;
  status?: string | null;
  connected?: boolean | null;
}

/**
 * The sensor class that mirrors `InstrumentState` (`tunnel_current` is one of them).
 *
 * The class exists to re-report instrument state as sensors without any TCP traffic, so
 * every quantity it reports is already drawn by the INSTRUMENT section above: the same
 * number would appear twice in the same side panel.
 *
 * The filter keys on the driver class, not on the name `tunnel_current`: the class can be
 * constructed with other attrs and names (`name=` / `attr=` are constructor arguments),
 * so a name filter would only catch today's instance.
 *
 * Display only. The sensor is still sampled, stored in environment history and used by
 * the criteria.
 */
export const INSTRUMENT_MIRROR_TYPE = "InstrumentStateSensor";

export function mirrorsInstrumentPanel(s: EnvSensor): boolean {
  return (s.type ?? "") === INSTRUMENT_MIRROR_TYPE;
}

/**
 * Placeholder implementation: not "cannot be read" but "MAST has no driver for this
 * quantity at all".
 *
 * Both currently have the same `status` (`unavailable`) yet are different facts: a vacuum
 * gauge with an unplugged COM port works again once plugged in, whereas `NoiseSensor`
 * never returned a number and no discovery flow can fill it in.
 *
 * "Unknown" and "absent" must be reported differently, otherwise a permanently
 * unavailable Noise Level looks broken. So placeholders get no reading row and are
 * reported once at the end as "not connected".
 *
 * The flag comes from the backend `SensorEntry.placeholder` (an isinstance check there,
 * not a class-name string).
 */
export function isStub(s: EnvSensor): boolean {
  return s.placeholder === true;
}

/** Sensors the panel actually draws as a reading row. */
export function readingRows(sensors: readonly EnvSensor[]): EnvSensor[] {
  return sensors.filter((s) => !mirrorsInstrumentPanel(s) && !isStub(s));
}

/**
 * The live sensors behind one headline slot (vacuum / temperature / ...).
 *
 * Empty means the slot has no live reading. The follow-up question is "not connected, or
 * connected but unreadable": see {@link stubKinds}.
 */
export function headlineSensors(sensors: readonly EnvSensor[], kind: string): EnvSensor[] {
  return readingRows(sensors).filter((s) => (s.kind ?? "") === kind && s.connected === true);
}

/**
 * Headline slots backed only by placeholders: these are what the "not connected" line at
 * the end of the panel lists.
 *
 * A slot with a real sensor (even one currently unreadable) does not count: it keeps
 * showing N/A and waits for the sensor to return; calling it "not connected" would be
 * wrong in the other direction.
 */
export function stubKinds(sensors: readonly EnvSensor[], kinds: readonly string[]): string[] {
  const live = new Set(readingRows(sensors).map((s) => s.kind ?? ""));
  const stubbed = new Set(sensors.filter(isStub).map((s) => s.kind ?? ""));
  return kinds.filter((k) => stubbed.has(k) && !live.has(k));
}
