// Imperative uPlot: the live charts do not use uplot-react.
//
// In uplot-react the data is a prop, so every frame triggers a React render, a
// comparison and a setData. At 20 Hz across several charts React itself becomes
// the bottleneck. Here the uPlot instance is rebuilt only when the chart structure
// changes (channels added or removed, theme, units); the caller pushes data with
// setData directly from the frame-coalescing callback.
//
// Zoom: after the user drag-zooms, incoming data must not reset the zoom
// (otherwise a live chart could not be zoomed at all). Double-click restores the
// automatic range (a built-in uPlot gesture); this hook only remembers whether the
// chart is currently zoomed.

import { useCallback, useEffect, useRef, useState, type DependencyList, type MutableRefObject } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { useElementWidth, useViewportHeight } from "@/hooks/useElementWidth";
import { sizeChart, type SizeChartOpts } from "@/lib/chartSize";
import { fmtSI } from "@/lib/units";

export interface UplotHandle {
  /** Callback ref of the container. */
  ref: (node: HTMLDivElement | null) => void;
  plot: MutableRefObject<uPlot | null>;
  /** The user drag-zoomed: new data does not reset the x range. */
  zoomed: MutableRefObject<boolean>;
  width: number;
  height: number;
  /**
   * Incremented each time a uPlot instance is built. The instance is built in an
   * effect and building it does not re-render; callers put this in the draw
   * dependencies, otherwise a stream that has ended (no more frames) would stay an
   * empty chart.
   */
  gen: number;
  /** Put data into the chart (keeping the zoom). */
  setData: (data: uPlot.AlignedData) => void;
}

export function useUplot(
  build: (width: number, height: number) => uPlot.Options,
  initialData: () => uPlot.AlignedData,
  deps: DependencyList,
  size: { fallbackW: number } & SizeChartOpts,
): UplotHandle {
  const [measureRef, measuredW] = useElementWidth<HTMLDivElement>();
  const viewportH = useViewportHeight();
  const { width, height } = sizeChart(measuredW, size.fallbackW, { ...size, viewportH });
  const [node, setNode] = useState<HTMLDivElement | null>(null);
  const [gen, setGen] = useState(0);
  const plot = useRef<uPlot | null>(null);
  const zoomed = useRef(false);
  const buildRef = useRef(build);
  buildRef.current = build;
  const dataRef = useRef(initialData);
  dataRef.current = initialData;
  const sizeRef = useRef({ width, height });
  sizeRef.current = { width, height };

  const ref = useCallback(
    (el: HTMLDivElement | null) => {
      measureRef(el);
      setNode(el);
    },
    [measureRef],
  );

  useEffect(() => {
    if (!node) return;
    const opts = buildRef.current(sizeRef.current.width, sizeRef.current.height);
    const userHooks = opts.hooks ?? {};
    opts.hooks = {
      ...userHooks,
      setSelect: [
        ...(userHooks.setSelect ?? []),
        (u: uPlot) => {
          if (u.select.width > 0) zoomed.current = true;
        },
      ],
    };
    const u = new uPlot(opts, dataRef.current(), node);
    // Double-click restores the automatic range (uPlot default); clear the "zoomed" flag in step
    const onDbl = () => {
      zoomed.current = false;
    };
    u.over.addEventListener("dblclick", onDbl);
    plot.current = u;
    zoomed.current = false;
    setGen((g) => g + 1);
    return () => {
      u.over.removeEventListener("dblclick", onDbl);
      u.destroy();
      if (plot.current === u) plot.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node, ...deps]);

  useEffect(() => {
    plot.current?.setSize({ width, height });
  }, [width, height]);

  const setData = useCallback((data: uPlot.AlignedData) => {
    const u = plot.current;
    if (!u) return;
    if (!zoomed.current) {
      u.setData(data, true);
      return;
    }
    // Zoomed: keep the x range, the y axis still follows the data
    const x = u.scales.x;
    const keep = x && x.min != null && x.max != null ? { min: x.min, max: x.max } : null;
    u.batch(() => {
      u.setData(data, false);
      if (keep) u.setScale("x", keep);
    });
  }, []);

  return { ref, plot, zoomed, width, height, gen, setData };
}

/** Common axis style: hairline solid grid, text in the text colour rather than a data colour. */
export function axisStyle(theme: { axis: string; grid: string; tick: string }): Partial<uPlot.Axis> {
  return {
    stroke: theme.axis,
    grid: { stroke: theme.grid, width: 1 },
    ticks: { stroke: theme.tick, width: 1 },
  };
}

/** Very small / large numbers all collapse to "0" with the default ticks (currents of order 1e-12 A); use exponent notation instead. */
export function sciValues(_u: uPlot, splits: number[]): string[] {
  const span = splits.length > 1 ? Math.abs(splits[splits.length - 1]! - splits[0]!) : 0;
  const big = splits.some((v) => Math.abs(v) >= 1e5);
  const tiny = span > 0 && span < 1e-3;
  if (big || tiny) return splits.map((v) => (v === 0 ? "0" : v.toExponential(1)));
  return splits.map((v) => String(Number.parseFloat(v.toPrecision(6))));
}

/** Units whose ticks are written with engineering prefixes (100 pA and 2 fA read better than 1e-10). */
const SI_TICK_UNITS = new Set(["A", "V", "m", "Hz", "s", "W", "Ω", "N", "T"]);

/**
 * Tick text: for SI base units use engineering prefixes (fmtSI), adding digits
 * until adjacent ticks no longer collide; other units fall back to sciValues.
 */
export function unitValues(unit: string | null | undefined): (u: uPlot, splits: number[]) => string[] {
  const unitStr = (unit ?? "").trim();
  if (!SI_TICK_UNITS.has(unitStr)) return sciValues;
  return (_u, splits) => {
    for (let digits = 3; digits <= 6; digits++) {
      const out = splits.map((v) => (v === 0 ? "0" : fmtSI(v, unitStr, { digits })));
      if (new Set(out).size === out.length) return out;
    }
    return splits.map((v) => (v === 0 ? "0" : fmtSI(v, unitStr, { digits: 6 })));
  };
}
