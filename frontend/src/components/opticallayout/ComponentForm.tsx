import { useMemo, useState } from "react";
import clsx from "clsx";
import { tr } from "@/lib/i18n";
import { Badge } from "@/components/ui";
import { Checkbox } from "@/components/controls";
import { parseNumberInput } from "@/lib/operatorPrompts";
import type { Annotations, ComponentType, LayoutComponent, Links, OpticalLayout } from "@/lib/opticalLayout/types";
import { COMPONENT_TYPES, LIMITS } from "@/lib/opticalLayout/types";
import { CATALOG, angleMeaning, propFields, typeLabel, type PropField } from "@/lib/opticalLayout/catalog";
import {
  changeType,
  getComponent,
  idProblem,
  renameComponent,
  setAngle,
  setAnnotation,
  setAperture,
  setLabel,
  setLinks,
  linkFibre,
  setLocked,
  setPosition,
  setProp,
  setSourceWavelengths,
} from "@/lib/opticalLayout/edit";
import { arrivingWavelengths, ownWavelengths, truncation, type DetailedTrace } from "@/lib/opticalLayout/trace";
import { isAimable } from "@/lib/opticalLayout/align";
import { nearestHole } from "@/lib/opticalLayout/geometry";
import type { HitIndicator } from "@/lib/opticalLayout/indicators";
import { issuesFor, type Issue } from "@/lib/opticalLayout/validate";
import { wavelengthLabel } from "@/lib/opticalLayout/colors";
import type { LinksState } from "@/lib/opticalLayout/types";
import type { OpticalSlot, SlotValue } from "@/lib/opticalState";
import { CommitNumber, CommitText, CommitTextarea, Group, INPUT_CLS, NumberRow, Row } from "./fields";
import { LinkFields, type DeviceOption } from "./LinkFields";

// The inspector of one selected component: geometry to 0.01 mm / 0.01 degree, the type's
// parameters, the alignment of the beams that hit it, the annotations people and the LLM
// read (model, maker, function, motorized, actuator, connection, datasheet, notes,
// extra), and the links to a device and a manual state slot.

export interface InspectorActions {
  aim: (mirrorId: string) => void;
  putOnBeam: (id: string) => string | null;
  placeAlong: (id: string, afterId: string, distanceMm: number, alignAxis: boolean) => string | null;
  remove: (ids: string[]) => void;
  duplicate: (ids: string[]) => void;
  select: (ids: string[]) => void;
  /** a one-line notice under the toolbar (what an edit did to a component other than the selected one) */
  notify: (text: string) => void;
}

export interface LinkSources {
  devices: readonly DeviceOption[] | null;
  deviceError: string | null;
  slots: readonly OpticalSlot[] | null;
  slotValues: Readonly<Record<string, SlotValue>> | null;
  slotError: string | null;
  snapshot: LinksState;
}

export interface InspectorCtx {
  layout: OpticalLayout;
  trace: DetailedTrace;
  indicators: ReadonlyMap<string, HitIndicator[]>;
  issues: readonly Issue[];
  readOnly: boolean;
  edit: (fn: (l: OpticalLayout) => OpticalLayout, key?: string) => void;
  actions: InspectorActions;
  links: LinkSources;
  nowMs: number;
  commentCount: number;
}

const SELECT_CLS = INPUT_CLS;

function PropEditor({ c, field, ctx }: { c: LayoutComponent; field: PropField; ctx: InspectorCtx }) {
  const { edit, readOnly, layout, issues } = ctx;
  const v = c.props[field.key];
  const problem = issuesFor(issues, c.id, `.props.${field.key}`)[0]?.message ?? null;
  const set = (value: unknown) => {
    if (c.type === "fiber_in" && field.key === "continues_at") return linkTo(typeof value === "string" ? value : undefined);
    edit((l) => setProp(l, c.id, field.key, value));
  };
  /** the far end of a fibre: the output takes the wavelength that arrives here, when nobody has set its own */
  const linkTo = (outId: string | undefined) => {
    const r = linkFibre(layout, c.id, outId, arrivingWavelengths(ctx.trace, c.id));
    edit(() => r.layout);
    if (outId && r.wavelengths) {
      const out = getComponent(layout, outId);
      const nm = r.wavelengths.join(", ");
      ctx.actions.notify(tr(`光纤输出 ${out?.label || outId} 的波长设为 ${nm} nm（到达光纤输入的光）。`, `The wavelength of the fibre output ${out?.label || outId} is now ${nm} nm, what arrives at the fibre input.`));
    }
  };
  const label = field.unit ? `${field.label} (${field.unit})` : field.label;

  switch (field.kind) {
    case "number":
      return (
        <Row label={label} hint={field.hint} error={problem}>
          <CommitNumber value={typeof v === "number" ? v : null} onCommit={(n) => set(n)} rules={{ min: field.min, max: field.max, minExclusive: field.minExclusive, optional: !field.required }} disabled={readOnly} aria-label={field.label} />
        </Row>
      );
    case "numberList": {
      const text = Array.isArray(v) ? v.filter((x) => typeof x === "number").join(", ") : "";
      return (
        <Row label={label} hint={field.hint} error={problem}>
          <CommitText
            value={text}
            mono
            disabled={readOnly}
            invalid={problem !== null}
            aria-label={field.label}
            placeholder="532, 1064"
            onCommit={(raw) => {
              if (raw.trim() === "") return set(undefined);
              const parts = raw.split(/[;,\s]+/).filter(Boolean);
              const nums = parts.map((p) => parseNumberInput(p));
              if (nums.some((n) => !n.ok)) return;
              set(nums.map((n) => (n.ok ? n.value : 0)));
            }}
          />
        </Row>
      );
    }
    case "enum":
      return (
        <Row label={label} hint={field.hint} error={problem}>
          <select className={SELECT_CLS} disabled={readOnly} value={typeof v === "string" ? v : ""} aria-label={field.label} onChange={(e) => set(e.target.value === "" ? undefined : e.target.value)}>
            {!field.required && <option value="">{tr("（未设置）", "(not set)")}</option>}
            {field.required && typeof v !== "string" && <option value="">{tr("（请选择）", "(choose)")}</option>}
            {field.options?.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Row>
      );
    case "bool":
      return (
        <Row label={label} hint={field.hint} error={problem}>
          <div className="pt-1">
            <Checkbox checked={v === true} onChange={(b) => !readOnly && set(b)} label={v === true ? tr("是", "Yes") : tr("否", "No")} />
          </div>
        </Row>
      );
    case "componentRef": {
      const options = layout.components.filter((x) => x.type === field.refType && x.id !== c.id);
      return (
        <Row label={label} hint={field.hint} error={problem}>
          <select className={SELECT_CLS} disabled={readOnly} value={typeof v === "string" ? v : ""} aria-label={field.label} onChange={(e) => set(e.target.value === "" ? undefined : e.target.value)}>
            <option value="">{tr("（无）", "(none)")}</option>
            {typeof v === "string" && !options.some((o) => o.id === v) && <option value={v}>{v}</option>}
            {options.map((o) => (
              <option key={o.id} value={o.id}>
                {o.label || o.id} ({o.id})
              </option>
            ))}
          </select>
        </Row>
      );
    }
  }
}

function HitList({ hits, aperture, cut }: { hits: readonly HitIndicator[]; aperture: number; cut: boolean }) {
  if (hits.length === 0) {
    // a cut trace cannot say that nothing reaches it: the light that would is the light that was not traced
    return <p className="text-xs text-mast-faint">{cut ? tr("追迹被截断，不知道有没有光打到它。", "The trace is cut: it is not known whether a beam reaches it.") : tr("现在没有光打到它。", "No beam reaches it right now.")}</p>;
  }
  const TONE = { ok: "border-mast-auto", warn: "border-mast-warn", bad: "border-mast-danger" } as const;
  return (
    <ul className="space-y-1">
      {hits.map((h) => (
        <li key={`${h.source}|${h.wavelength_nm}|${h.branch}|${h.path_mm}`} className={clsx("border-l-2 pl-2 text-xs", TONE[h.level])}>
          <div className="flex flex-wrap items-baseline gap-x-2 text-mast-text">
            <span className="font-medium">{h.source}</span>
            <span>{wavelengthLabel(h.wavelength_nm)}</span>
            <span className="text-mast-faint">{tr("分支", "branch")} {h.branch}</span>
            {h.fraction < 0.999 && <span className="text-mast-faint">{Math.round(h.fraction * 100)}%</span>}
          </div>
          <div className="font-mono tabular-nums text-mast-muted">
            Δ {h.offset_mm.toFixed(2)} mm ({Math.round((Math.abs(h.offset_mm) / aperture) * 100)}%) · {h.incidence_deg.toFixed(1)}° · {tr("路径", "path")} {h.path_mm.toFixed(1)} mm (+{h.leg_mm.toFixed(1)})
          </div>
          {h.reason === "back_hit" && <div className="text-mast-danger">{tr("光打在背面。", "The beam arrives at the back.")}</div>}
          {h.reason === "blocked" && <div className="text-mast-warn">{tr("光在这里被挡住。", "The beam is blocked here.")}</div>}
          {h.reason === "off_center" && <div className="text-mast-warn">{tr("偏离中心超过口径的 20%。", "More than 20% of the aperture off centre.")}</div>}
        </li>
      ))}
    </ul>
  );
}

function PlaceAlong({ c, ctx }: { c: LayoutComponent; ctx: InspectorCtx }) {
  const others = ctx.layout.components.filter((x) => x.id !== c.id);
  const [after, setAfter] = useState("");
  const [dist, setDist] = useState("50");
  const [axis, setAxis] = useState(true);
  const [msg, setMsg] = useState<string | null>(null);
  const afterId = others.some((o) => o.id === after) ? after : (others[0]?.id ?? "");
  if (others.length === 0) return null;
  return (
    <div className="space-y-1.5 rounded-mast-ctl border border-mast-border p-2">
      <div className="text-xs font-medium text-mast-text">{tr("沿光束放置", "Place along the beam")}</div>
      <div className="flex flex-wrap items-center gap-1.5 text-xs text-mast-muted">
        <span>{tr("在", "Behind")}</span>
        <select className={clsx(SELECT_CLS, "!w-auto max-w-[9rem]")} value={afterId} onChange={(e) => setAfter(e.target.value)} aria-label={tr("放在哪个器件之后", "Behind which component")}>
          {others.map((o) => (
            <option key={o.id} value={o.id}>
              {o.label || o.id}
            </option>
          ))}
        </select>
        <span>{tr("之后", "by")}</span>
        <input className={clsx(INPUT_CLS, "!w-16 font-mono")} value={dist} onChange={(e) => setDist(e.target.value)} inputMode="decimal" aria-label={tr("沿光束的距离 (mm)", "Distance along the beam (mm)")} />
        <span>mm</span>
      </div>
      <Checkbox checked={axis} onChange={setAxis} label={tr("同时把光轴转到沿光束", "Also turn the axis along the beam")} />
      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={ctx.readOnly}
          className="rounded-mast-ctl border border-mast-border-strong px-2.5 py-1 text-xs text-mast-text hover:bg-mast-panel-2 disabled:opacity-50"
          onClick={() => {
            const n = parseNumberInput(dist);
            if (!n.ok) return setMsg(n.error);
            if (n.value < 0) return setMsg(tr("距离不能为负。", "The distance cannot be negative."));
            setMsg(ctx.actions.placeAlong(c.id, afterId, n.value, axis));
          }}
        >
          {tr("放过去", "Place")}
        </button>
        {msg && <span className="text-xs text-mast-warn">{msg}</span>}
      </div>
    </div>
  );
}

export function ComponentForm({ c, ctx }: { c: LayoutComponent; ctx: InspectorCtx }) {
  const { layout, edit, readOnly, actions, issues } = ctx;
  const hits = ctx.indicators.get(c.id) ?? [];
  const hole = useMemo(() => nearestHole(layout.table, { x: c.x_mm, y: c.y_mm }), [layout.table, c.x_mm, c.y_mm]);
  const [idMsg, setIdMsg] = useState<string | null>(null);
  const [beamMsg, setBeamMsg] = useState<string | null>(null);
  const a = c.annotations;
  const setA = (patch: Partial<Annotations>) => edit((l) => setAnnotation(l, c.id, patch));
  const setL = (patch: Partial<Links>) => edit((l) => setLinks(l, c.id, patch));
  const aIssue = (key: string) => issuesFor(issues, c.id, `.annotations.${key}`)[0]?.message ?? null;
  const fields = propFields(c.type);
  const feeder = c.type === "fiber_out" ? layout.components.find((o) => o.type === "fiber_in" && o.props["continues_at"] === c.id) : undefined;
  const arriving = feeder ? arrivingWavelengths(ctx.trace, feeder.id) : [];
  const own = ownWavelengths(c).filter((w): w is number => w !== null);
  const mismatch = arriving.length > 0 && !(arriving.length === own.length && arriving.every((w) => own.includes(w)));

  const onAtHole = hole !== null && hole.dx === 0 && hole.dy === 0;

  return (
    <div className="space-y-0" data-inspector="component">
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <Badge>{typeLabel(c.type)}</Badge>
        <code className="rounded bg-mast-code-bg px-1 text-xs text-mast-text">{c.id}</code>
        {c.locked && <Badge tone="WARN">{tr("已锁定", "Locked")}</Badge>}
        {a.motorized && <Badge tone="INFO">{tr("电动", "Motorized")}</Badge>}
        {ctx.commentCount > 0 && <Badge tone="INFO">{tr(`${ctx.commentCount} 条未解决评论`, `${ctx.commentCount} open comments`)}</Badge>}
      </div>

      <Group title={tr("基本", "Basics")}>
        <Row label={tr("类型", "Type")}>
          <select className={SELECT_CLS} disabled={readOnly} value={c.type} aria-label={tr("器件类型", "Component type")} onChange={(e) => edit((l) => changeType(l, c.id, e.target.value as ComponentType))}>
            {COMPONENT_TYPES.map((t) => (
              <option key={t} value={t}>
                {typeLabel(t)}
              </option>
            ))}
          </select>
        </Row>
        <Row label={tr("编号", "Id")} help={tr("评论按编号引用器件；改编号后旧评论仍指向旧编号。", "Comments refer to a component by its id; after a rename old comments keep the old id.")} error={idMsg}>
          <CommitText
            value={c.id}
            mono
            disabled={readOnly}
            invalid={idMsg !== null}
            maxLength={LIMITS.idMax}
            aria-label={tr("编号", "Id")}
            onCommit={(v) => {
              const problem = idProblem(layout, v, c.id);
              if (problem === "format") return setIdMsg(tr("只能含字母、数字、下划线和连字符，1 到 40 个字符。", "Letters, digits, underscore and hyphen only, 1 to 40 characters."));
              if (problem === "taken") return setIdMsg(tr("这个编号已被占用。", "That id is already used."));
              setIdMsg(null);
              edit((l) => renameComponent(l, c.id, v));
              actions.select([v]);
            }}
          />
        </Row>
        <Row label={tr("名称", "Label")}>
          <CommitText value={c.label} disabled={readOnly} maxLength={LIMITS.labelMax} aria-label={tr("名称", "Label")} onCommit={(v) => edit((l) => setLabel(l, c.id, v))} />
        </Row>
        <Row label={tr("锁定", "Lock")} help={tr("锁定后拖动、方向键、旋转和删除都不会动它；在这里改数值仍可以。", "A locked component ignores dragging, arrow keys, rotation and delete; typing numbers here still works.")}>
          <div className="pt-1">
            <Checkbox checked={c.locked} onChange={(b) => !readOnly && edit((l) => setLocked(l, [c.id], b))} label={c.locked ? tr("已锁定", "Locked") : tr("未锁定", "Unlocked")} />
          </div>
        </Row>
      </Group>

      <Group title={tr("位置与方向", "Position and direction")}>
        <NumberRow label="x" unit="mm" value={c.x_mm} disabled={readOnly} rules={{ step: 1, min: -LIMITS.coordAbs, max: LIMITS.coordAbs }} onCommit={(v) => v !== null && edit((l) => setPosition(l, c.id, v, getComponent(l, c.id)?.y_mm ?? c.y_mm))} />
        <NumberRow label="y" unit="mm" value={c.y_mm} disabled={readOnly} rules={{ step: 1, min: -LIMITS.coordAbs, max: LIMITS.coordAbs }} onCommit={(v) => v !== null && edit((l) => setPosition(l, c.id, getComponent(l, c.id)?.x_mm ?? c.x_mm, v))} />
        <NumberRow label={tr("角度", "Angle")} unit="°" value={c.angle_deg} disabled={readOnly} hint={angleMeaning(c.type)} rules={{ step: 1 }} onCommit={(v) => v !== null && edit((l) => setAngle(l, c.id, v))} />
        <NumberRow label={tr("口径", "Aperture")} unit="mm" value={c.aperture_mm} disabled={readOnly} rules={{ step: 1, min: LIMITS.apertureMin, max: LIMITS.apertureMax }} onCommit={(v) => v !== null && edit((l) => setAperture(l, c.id, v))} />
        {hole && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-mast-muted">
            <span>
              {tr(`最近的孔：第 ${hole.row} 行、第 ${hole.col} 列，偏 Δx ${hole.dx.toFixed(2)}、Δy ${hole.dy.toFixed(2)} mm`, `Nearest hole: row ${hole.row}, column ${hole.col}, off Δx ${hole.dx.toFixed(2)}, Δy ${hole.dy.toFixed(2)} mm`)}
            </span>
            <button
              type="button"
              disabled={readOnly || onAtHole}
              className="rounded-mast-ctl border border-mast-border-strong px-2 py-0.5 text-xs text-mast-text hover:bg-mast-panel-2 disabled:opacity-50"
              onClick={() => edit((l) => setPosition(l, c.id, hole.x, hole.y))}
            >
              {tr("吸到孔上", "Snap to hole")}
            </button>
          </div>
        )}
      </Group>

      <Group title={tr("对准工具", "Alignment tools")} defaultOpen={false}>
        <div className="flex flex-wrap gap-1.5">
          {isAimable(c) && (
            <button type="button" disabled={readOnly} className="rounded-mast-ctl border border-mast-border-strong px-2.5 py-1 text-xs text-mast-text hover:bg-mast-panel-2 disabled:opacity-50" onClick={() => actions.aim(c.id)} title={tr("再点目标器件：把镜子转到让光反射后正好指向它", "Then click the target: the mirror turns so the beam is reflected exactly at it")}>
              {tr("瞄准…", "Aim…")}
            </button>
          )}
          <button
            type="button"
            disabled={readOnly || CATALOG[c.type].role === "source"}
            className="rounded-mast-ctl border border-mast-border-strong px-2.5 py-1 text-xs text-mast-text hover:bg-mast-panel-2 disabled:opacity-50"
            onClick={() => setBeamMsg(actions.putOnBeam(c.id))}
            title={tr("把它移到光束线上离它最近的点", "Move it to the nearest point of the beams")}
          >
            {tr("放到光束上", "Put on the beam")}
          </button>
        </div>
        {beamMsg && <p className="text-xs text-mast-warn">{beamMsg}</p>}
        {CATALOG[c.type].role !== "source" && <PlaceAlong c={c} ctx={ctx} />}
      </Group>

      {fields.length > 0 && (
        <Group title={tr("类型参数", "Parameters")}>
          {feeder && (
            <p className="rounded-mast-ctl border border-mast-info-border bg-mast-info-bg px-2 py-1 text-xs text-mast-text">
              {tr(
                `它是光纤耦合 ${feeder.label || feeder.id} 的另一端：追迹时它只发到达那里的光，不用自己的波长设置（服务器仍要求填一个）。`,
                `It is the far end of the fibre input ${feeder.label || feeder.id}: the trace lights it with what arrives there and ignores its own wavelength (the server still wants one).`,
              )}
              {mismatch && (
                <>
                  {" "}
                  {tr(`到达那里的光是 ${arriving.join(", ")} nm，这里写的是 ${own.join(", ") || "—"} nm。`, `What arrives there is ${arriving.join(", ")} nm; this says ${own.join(", ") || "none"} nm.`)}{" "}
                  <button type="button" disabled={readOnly} className="text-mast-accent underline disabled:opacity-50" onClick={() => edit((l) => setSourceWavelengths(l, c.id, arriving))}>
                    {tr("改成到达的波长", "Use the arriving wavelength")}
                  </button>
                </>
              )}
            </p>
          )}
          {fields.map((f) => (
            <PropEditor key={f.key} c={c} field={f} ctx={ctx} />
          ))}
        </Group>
      )}

      <Group title={tr("对准情况", "Alignment")} badge={hits.length > 0 ? <Badge tone={hits.some((h) => h.level === "bad") ? "DANGEROUS" : hits.some((h) => h.level === "warn") ? "WARN" : "AUTO"}>{hits.length}</Badge> : undefined}>
        <HitList cut={truncation(ctx.trace) !== null} hits={hits} aperture={c.type === "iris" && typeof c.props["open_mm"] === "number" && (c.props["open_mm"] as number) > 0 ? (c.props["open_mm"] as number) : c.aperture_mm} />
      </Group>

      <Group title={tr("注释", "Annotations")}>
        <Row label={tr("型号", "Model")} error={aIssue("model")}>
          <CommitText value={a.model} disabled={readOnly} maxLength={LIMITS.stringMax} aria-label={tr("型号", "Model")} onCommit={(v) => setA({ model: v })} />
        </Row>
        <Row label={tr("厂家", "Manufacturer")} error={aIssue("manufacturer")}>
          <CommitText value={a.manufacturer} disabled={readOnly} maxLength={LIMITS.stringMax} aria-label={tr("厂家", "Manufacturer")} onCommit={(v) => setA({ manufacturer: v })} />
        </Row>
        <Row label={tr("功能", "Function")} error={aIssue("function")}>
          <CommitText value={a.function} disabled={readOnly} maxLength={LIMITS.stringMax} placeholder={tr("如：转折、滤掉泵浦光", "e.g. steering, pump filter")} aria-label={tr("功能", "Function")} onCommit={(v) => setA({ function: v })} />
        </Row>
        <Row label={tr("电动", "Motorized")}>
          <div className="pt-1">
            <Checkbox checked={a.motorized} onChange={(b) => !readOnly && setA({ motorized: b })} label={a.motorized ? tr("是", "Yes") : tr("否", "No")} />
          </div>
        </Row>
        <Row label={tr("电机/控制器", "Actuator")} hint={a.motorized ? undefined : tr("勾选“电动”后填写更有用", "Most useful when Motorized is ticked")} error={aIssue("actuator")}>
          <CommitText value={a.actuator} disabled={readOnly} maxLength={LIMITS.stringMax} placeholder={tr("如：K10CR1", "e.g. K10CR1")} aria-label={tr("电机或控制器型号", "Motor or controller model")} onCommit={(v) => setA({ actuator: v })} />
        </Row>
        <Row label={tr("接到哪里", "Connected to")} error={aIssue("connection")}>
          <CommitText value={a.connection} disabled={readOnly} maxLength={LIMITS.stringMax} placeholder={tr("控制器、端口、通道、卫星、COM 口…", "controller, port, channel, satellite, COM port…")} aria-label={tr("接到哪里", "Connected to")} onCommit={(v) => setA({ connection: v })} />
        </Row>
        <Row label={tr("数据手册", "Datasheet")} error={aIssue("datasheet_url")}>
          <div className="flex items-center gap-1.5">
            <CommitText value={a.datasheet_url} disabled={readOnly} maxLength={LIMITS.stringMax} placeholder="https://…" mono aria-label={tr("数据手册链接", "Datasheet link")} onCommit={(v) => setA({ datasheet_url: v })} />
            {/^https?:\/\//i.test(a.datasheet_url) && (
              <a href={a.datasheet_url} target="_blank" rel="noreferrer noopener" className="shrink-0 text-xs text-mast-accent underline">
                {tr("打开", "Open")}
              </a>
            )}
          </div>
        </Row>
        <Row label={tr("备注", "Notes")} error={aIssue("notes")} hint={`${a.notes.length} / ${LIMITS.notesMax}`}>
          <CommitTextarea value={a.notes} disabled={readOnly} maxLength={LIMITS.notesMax} rows={3} onCommit={(v) => setA({ notes: v })} />
        </Row>
        <Row label={tr("其他", "Extra")} wide hint={aIssue("extra")}>
          <div className="space-y-1">
            {a.extra.map((e, i) => (
              <div key={i} className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)_auto] items-center gap-1">
                <CommitText value={e.key} disabled={readOnly} maxLength={LIMITS.extraKeyMax} placeholder={tr("名称", "name")} aria-label={tr("补充项名称", "Extra name")} onCommit={(v) => setA({ extra: a.extra.map((x, j) => (j === i ? { ...x, key: v } : x)) })} />
                <CommitText value={e.value} disabled={readOnly} maxLength={LIMITS.extraValueMax} placeholder={tr("内容", "value")} aria-label={tr("补充项内容", "Extra value")} onCommit={(v) => setA({ extra: a.extra.map((x, j) => (j === i ? { ...x, value: v } : x)) })} />
                <button type="button" disabled={readOnly} className="px-1.5 text-mast-muted hover:text-mast-danger disabled:opacity-50" aria-label={tr("删除这一项", "Remove this entry")} onClick={() => setA({ extra: a.extra.filter((_, j) => j !== i) })}>
                  ✕
                </button>
              </div>
            ))}
            <button
              type="button"
              disabled={readOnly || a.extra.length >= LIMITS.extraPairs}
              className="rounded-mast-ctl border border-dashed border-mast-border px-2 py-0.5 text-xs text-mast-muted hover:border-mast-border-strong hover:text-mast-text disabled:opacity-50"
              onClick={() => setA({ extra: [...a.extra, { key: "", value: "" }] })}
            >
              + {tr("加一项", "Add an entry")}
            </button>
          </div>
        </Row>
      </Group>

      <Group title={tr("链接", "Links")}>
        <LinkFields
          links={c.links}
          devices={ctx.links.devices}
          deviceError={ctx.links.deviceError}
          slots={ctx.links.slots}
          slotValues={ctx.links.slotValues}
          slotError={ctx.links.slotError}
          snapshot={ctx.links.snapshot[c.id]}
          disabled={readOnly}
          onChange={setL}
          nowMs={ctx.nowMs}
        />
      </Group>

      <div className="flex gap-2 border-t border-mast-border pt-3">
        <button type="button" disabled={readOnly} className="rounded-mast-ctl border border-mast-border-strong px-3 py-1 text-sm text-mast-text hover:bg-mast-panel-2 disabled:opacity-50" onClick={() => actions.duplicate([c.id])}>
          {tr("复制", "Duplicate")}
        </button>
        <button type="button" disabled={readOnly || c.locked} title={c.locked ? tr("已锁定，先解锁", "Locked: unlock it first") : undefined} className="rounded-mast-ctl border border-mast-danger-border bg-mast-danger-bg px-3 py-1 text-sm font-semibold text-mast-danger hover:opacity-90 disabled:opacity-50" onClick={() => actions.remove([c.id])}>
          {tr("删除", "Delete")}
        </button>
      </div>
    </div>
  );
}

/** A few components selected at once: what applies to all of them. */
export function GroupForm({ ids, ctx }: { ids: readonly string[]; ctx: InspectorCtx }) {
  const comps = ids.map((id) => getComponent(ctx.layout, id)).filter((c): c is LayoutComponent => c !== undefined);
  const locked = comps.filter((c) => c.locked).length;
  return (
    <div className="space-y-3" data-inspector="group">
      <p className="text-sm text-mast-text">{tr(`已选 ${comps.length} 个器件`, `${comps.length} components selected`)}</p>
      <p className="text-xs text-mast-muted">{tr("拖动或用方向键一起移动；[ 和 ] 绕中心一起旋转；点任意一个只看它。", "Drag or use the arrow keys to move them together; [ and ] rotate them as a group about their centre; click one to look at it alone.")}</p>
      <ul className="max-h-48 space-y-0.5 overflow-auto text-sm">
        {comps.map((c) => (
          <li key={c.id}>
            <button type="button" className="flex w-full items-center justify-between rounded px-1.5 py-0.5 text-left hover:bg-mast-panel-2" onClick={() => ctx.actions.select([c.id])}>
              <span className="truncate text-mast-text">{c.label || c.id}</span>
              <span className="ml-2 shrink-0 text-xs text-mast-faint">{typeLabel(c.type)}{c.locked ? " 🔒" : ""}</span>
            </button>
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap gap-2">
        <button type="button" disabled={ctx.readOnly} className="rounded-mast-ctl border border-mast-border-strong px-3 py-1 text-sm text-mast-text hover:bg-mast-panel-2 disabled:opacity-50" onClick={() => ctx.edit((l) => setLocked(l, [...ids], true))}>
          {tr("全部锁定", "Lock all")}
        </button>
        <button type="button" disabled={ctx.readOnly || locked === 0} className="rounded-mast-ctl border border-mast-border-strong px-3 py-1 text-sm text-mast-text hover:bg-mast-panel-2 disabled:opacity-50" onClick={() => ctx.edit((l) => setLocked(l, [...ids], false))}>
          {tr("全部解锁", "Unlock all")}
        </button>
        <button type="button" disabled={ctx.readOnly} className="rounded-mast-ctl border border-mast-border-strong px-3 py-1 text-sm text-mast-text hover:bg-mast-panel-2 disabled:opacity-50" onClick={() => ctx.actions.duplicate([...ids])}>
          {tr("复制", "Duplicate")}
        </button>
        <button type="button" disabled={ctx.readOnly || locked === comps.length} className="rounded-mast-ctl border border-mast-danger-border bg-mast-danger-bg px-3 py-1 text-sm font-semibold text-mast-danger hover:opacity-90 disabled:opacity-50" onClick={() => ctx.actions.remove([...ids])}>
          {tr("删除", "Delete")}
        </button>
      </div>
      {locked > 0 && <p className="text-xs text-mast-faint">{tr(`其中 ${locked} 个已锁定，不会被移动、旋转或删除。`, `${locked} of them are locked and will not be moved, rotated or deleted.`)}</p>}
    </div>
  );
}
