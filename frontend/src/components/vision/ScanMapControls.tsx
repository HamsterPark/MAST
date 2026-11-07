import { useState } from "react";
import { tr } from "@/lib/i18n";
import { RadioGroup, Toggle } from "@/components/controls";
import { useUiStore } from "@/store";
import {
  MAP_LAYER_HINTS,
  MAP_LAYER_IDS,
  MAP_LAYER_LABELS,
  MAP_PRESETS,
  MAP_VIEW_MODES,
  matchPreset,
  type MapLayers,
  type MapViewMode,
} from "@/lib/scanMapView";

/**
 * The map's view controls: preset layer sets, per-layer switches, view range.
 *
 * The map draws eight kinds of overlay on one canvas, so each must be switchable.
 * Presets are the quick answer (a minimal set for a crowded surface, a planning
 * set when the question is where to go next); the switches underneath cover
 * every combination the presets do not.
 *
 * A preset is a SETTER, not a mode — clicking one writes the eight booleans and
 * then every switch is independent again, so the highlight is derived from the
 * booleans rather than stored. That is why flipping one switch simply leaves no
 * preset highlighted instead of needing a "custom" state that could drift out of
 * agreement with what is actually drawn.
 */
export function ScanMapControls({ compact = false }: { compact?: boolean }) {
  const layers = useUiStore((s) => s.mapLayers);
  const viewMode = useUiStore((s) => s.mapViewMode);
  const setLayer = useUiStore((s) => s.setMapLayer);
  const applyPreset = useUiStore((s) => s.applyMapPreset);
  const setViewMode = useUiStore((s) => s.setMapViewMode);
  const [open, setOpen] = useState(false);

  const active = matchPreset(layers);
  const hiddenCount = MAP_LAYER_IDS.filter((id) => !layers[id]).length;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="flex items-center gap-2">
          <span className="text-xs text-mast-muted">{tr("显示", "Show")}</span>
          <div className="inline-flex flex-wrap gap-[3px] rounded-mast-ctl border border-mast-border bg-mast-panel-2 p-[3px]">
            {MAP_PRESETS.map((p) => (
              <button
                key={p.id}
                type="button"
                title={p.hint}
                onClick={() => applyPreset(p.id)}
                aria-pressed={active === p.id}
                className={
                  "rounded-md px-3 py-1.5 text-sm transition-colors " +
                  (active === p.id
                    ? "bg-mast-accent font-semibold text-mast-accent-ink"
                    : "text-mast-muted hover:text-mast-text")
                }
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-xs text-mast-muted">{tr("范围", "Range")}</span>
          <RadioGroup<MapViewMode>
            value={viewMode}
            onChange={setViewMode}
            options={MAP_VIEW_MODES.map((m) => ({ value: m.id, label: m.label }))}
          />
        </div>

        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="rounded border border-dashed border-mast-border px-2 py-1 text-xs text-mast-muted hover:text-mast-text"
          aria-expanded={open}
        >
          {open ? "▾" : "▸"} {tr("图层", "Layers")}
          {hiddenCount > 0
            ? tr(` · 已隐藏 ${hiddenCount} 层`, ` · ${hiddenCount} hidden`)
            : ""}
        </button>
      </div>

      {open && (
        <div
          className={
            "grid gap-x-6 gap-y-2 rounded-mast-card border border-mast-border bg-mast-panel-2 p-3 " +
            (compact ? "sm:grid-cols-2" : "sm:grid-cols-2 lg:grid-cols-4")
          }
        >
          {MAP_LAYER_IDS.map((id) => (
            <LayerSwitch
              key={id}
              id={id}
              on={layers[id]}
              onChange={(v) => setLayer(id, v)}
              compact={compact}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function LayerSwitch({
  id,
  on,
  onChange,
  compact,
}: {
  id: keyof MapLayers;
  on: boolean;
  onChange: (v: boolean) => void;
  compact: boolean;
}) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="text-sm text-mast-text">{MAP_LAYER_LABELS[id]}</div>
        {!compact && (
          <div className="text-[11px] leading-tight text-mast-muted">{MAP_LAYER_HINTS[id]}</div>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {/* The knob position and accent colour must not be the only indicator
            of state — same rule the monitoring settings follow. */}
        <span className="text-[11px] text-mast-muted">{on ? tr("开", "On") : tr("关", "Off")}</span>
        <Toggle checked={on} onChange={onChange} label={MAP_LAYER_LABELS[id]} />
      </div>
    </div>
  );
}
