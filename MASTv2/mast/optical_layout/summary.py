"""The plain-language description of a layout, for an LLM (and for a person who wants it in words).

Contract: ``docs/v2/design/optical_layout.md`` §7. In order:

1. the title, the table, the number of components and sources, who saved it last and when;
2. for every light source and wavelength the components along the beam, in order - each with its
   model and manufacturer, position, how far off its centre the beam hits, the angle of
   incidence, the path travelled so far, whether it is motorized (and by what, connected where),
   what it is linked to in MAST and what that link says now; a split is written branch by branch;
3. the problems: a trace the whole-trace budget cut (first - and also as a line in the header, because
   the text is cut from its end - and from then on no conclusions from what no beam does: components
   no beam reaches, missed apertures, fibres without light), a beam on the back of a mirror, a hit more
   than 20 % of the aperture off center, a beam that passes just outside a component nothing else
   lights, a beam that leaves the table, components no beam reaches, a run that hit the event cap,
   links that point nowhere, and the unresolved comments;
4. with no light source at all: the components and their annotations instead.

The text is in the language ``tr`` picks (the request's, else ``MAST_LANG``), or the one asked for
with ``lang``. Longer than :data:`MAX_CHARS` it is cut and says so. Numbers: positions as they are
stored (0.01 mm), offsets, incidence angles and path lengths to 0.1.

Everything here is derived: it never writes anything and never touches an instrument.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from mast.i18n import is_zh, tr, use_lang
from mast.optical_layout.model import SOURCE_TYPES, Component, Layout, normalize_layout, type_name
from mast.optical_layout.trace import lit_components, near_misses, trace_layout, truncation

MAX_CHARS = 8000
#: a hit further than this share of the aperture from the centre is worth a line
OFF_CENTER_SHARE = 0.20
_MAX_PROBLEM_LINES = 12
_MAX_COMMENT_LINES = 10


# ── small formatting helpers ─────────────────────────────────────────────────────────────────


def _g(v: Any, nd: int = 2) -> str:
    """A number the way a person writes it: ``532``, ``62.5``, ``112.05``."""
    if v is None:
        return "?"
    text = f"{float(v):.{nd}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _f1(v: float) -> str:
    return f"{float(v):.1f}"


def _pos(c: Component) -> str:
    return f"({_g(c.x_mm)}, {_g(c.y_mm)})"


def _name(c: Component) -> str:
    """``M1 mirror`` / ``M1 反射镜`` (the label, else the id, then the type)."""
    return f"{c.label or c.id} {type_name(c.type)}"


def _grid(grid: str) -> str:
    return {"metric": tr("公制", "metric"), "imperial": tr("英制", "imperial"), "none": tr("无孔", "no holes")}.get(grid, grid)


def _wl(wavelength: Optional[float]) -> str:
    return f"{_g(wavelength)} nm" if wavelength is not None else tr("波长未知", "wavelength unknown")


def _comma() -> str:
    return "，" if is_zh() else ", "


def _paren(bits: Sequence[str]) -> str:
    if not bits:
        return ""
    return ("（" if is_zh() else " (") + _comma().join(bits) + ("）" if is_zh() else ")")


def _maker(c: Component) -> str:
    return " ".join(x for x in (c.annotations.manufacturer, c.annotations.model) if x)


def _source_wavelengths(c: Component) -> str:
    p = c.props
    values = p.get("wavelengths_nm") or ([p["wavelength_nm"]] if p.get("wavelength_nm") is not None else [])
    return ", ".join(_wl(v) for v in values) if values else tr("波长未知", "wavelength unknown")


# ── one component, in words ──────────────────────────────────────────────────────────────────


def _props_text(c: Component) -> list[str]:
    """What the type's parameters say."""
    p = c.props
    out: list[str] = []
    t = c.type
    if t in ("laser", "fiber_out") and p.get("power_mw") is not None:
        out.append(tr(f"功率 {_g(p['power_mw'])} mW", f"power {_g(p['power_mw'])} mW"))
    elif t == "beam_splitter":
        r = p.get("reflect_fraction", 0.5)
        out.append(tr(f"反射 {_g(r * 100, 1)}%", f"reflects {_g(r * 100, 1)}%"))
    elif t == "dichroic":
        short = p.get("reflects", "short") == "short"
        out.append(tr(f"截止 {_g(p.get('edge_nm'))} nm，反射{'短波' if short else '长波'}",
                      f"edge {_g(p.get('edge_nm'))} nm, reflects {'short' if short else 'long'} wavelengths"))
    elif t == "grating":
        lines = p.get("lines_per_mm")
        out.append(tr(f"{_g(lines)} 线/mm，" if lines is not None else "", f"{_g(lines)} lines/mm, " if lines is not None else "")
                   + tr("按零级反射处理", "treated as zero-order reflection"))
    elif t in ("lens", "objective") and p.get("focal_mm") is not None:
        out.append(tr(f"焦距 {_g(p['focal_mm'])} mm", f"focal length {_g(p['focal_mm'])} mm"))
    elif t == "waveplate":
        if p.get("retardance"):
            half = p["retardance"] == "half"
            out.append(tr("半波" if half else "四分之一波", "half-wave" if half else "quarter-wave"))
        if p.get("fast_axis_deg") is not None:
            out.append(tr(f"快轴 {_g(p['fast_axis_deg'])}°", f"fast axis {_g(p['fast_axis_deg'])} deg"))
    elif t == "polarizer" and p.get("axis_deg") is not None:
        out.append(tr(f"透光轴 {_g(p['axis_deg'])}°", f"axis {_g(p['axis_deg'])} deg"))
    elif t == "filter":
        lo, hi = p.get("pass_min_nm"), p.get("pass_max_nm")
        if lo is not None and hi is not None:
            out.append(tr(f"通带 {_g(lo)}–{_g(hi)} nm", f"passband {_g(lo)}-{_g(hi)} nm"))
        elif lo is not None:
            out.append(tr(f"通过 ≥ {_g(lo)} nm", f"passes >= {_g(lo)} nm"))
        elif hi is not None:
            out.append(tr(f"通过 ≤ {_g(hi)} nm", f"passes <= {_g(hi)} nm"))
    elif t == "nd" and p.get("od") is not None:
        out.append(f"OD {_g(p['od'])}")
    elif t == "iris" and p.get("open_mm") is not None:
        out.append(tr(f"开口 {_g(p['open_mm'])} mm", f"open {_g(p['open_mm'])} mm"))
    elif t == "shutter" and "closed" in p:
        out.append(tr("关闭" if p["closed"] else "打开", "closed" if p["closed"] else "open"))
    elif t == "sample" and "reflects" in p:
        out.append(tr("反射" if p["reflects"] else "不反射（终止）",
                      "reflecting" if p["reflects"] else "not reflecting (terminal)"))
    elif t == "custom" and p.get("behavior"):
        out.append(tr(f"行为 {p['behavior']}", f"behavior {p['behavior']}"))
    elif t == "fiber_in" and p.get("continues_at"):
        out.append(tr(f"光接到 {p['continues_at']}", f"light continues at {p['continues_at']}"))
    return out


def _link_text(c: Component, links: Optional[dict[str, dict[str, Any]]]) -> list[str]:
    """The references of a component and, when *links* (``links_state``) is given, what they say now."""
    out: list[str] = []
    state = links.get(c.id, {}) if links is not None else None
    if c.links.device_id:
        did = c.links.device_id
        dev = (state or {}).get("device")
        if dev is None:
            out.append(tr(f"MAST 设备 {did}", f"MAST device {did}"))
        elif dev.get("error"):
            out.append(tr(f"MAST 设备 {did}（状态读不到：{dev['error']}）", f"MAST device {did} (state unreadable: {dev['error']})"))
        elif dev.get("known") is False:
            out.append(tr(f"MAST 设备 {did}（设备清单里没有这台）", f"MAST device {did} (not in the device list)"))
        else:
            if dev.get("enabled") is False:
                word = tr("未启用", "disabled")
            elif dev.get("connected"):
                word = tr("已打开", "open")
            else:
                word = tr("未打开", "not open")
            if isinstance(dev.get("status"), dict):
                bits = [f"{k}={v}" for k, v in dev["status"].items() if isinstance(v, (str, int, float, bool))][:6]
                if bits:
                    word += ("；" if is_zh() else "; ") + ", ".join(bits)
            out.append(tr(f"MAST 设备 {did}（{word}）", f"MAST device {did} ({word})"))
    if c.links.slot:
        sid = c.links.slot
        slot = (state or {}).get("slot")
        if slot is None:
            out.append(tr(f"槽位 {sid}", f"slot {sid}"))
        elif slot.get("error"):
            out.append(tr(f"槽位 {sid}（读不到：{slot['error']}）", f"slot {sid} (unreadable: {slot['error']})"))
        elif slot.get("known") is False:
            out.append(tr(f"槽位 {sid}（没有这个槽位）", f"slot {sid} (no such slot)"))
        elif slot.get("value") is None:
            out.append(tr(f"槽位 {sid} 当前未签署", f"slot {sid} is not signed"))
        else:
            unit = f" {slot['unit']}" if slot.get("unit") else ""
            expired = "" if slot.get("valid") else tr("，已失效", ", expired")
            out.append(tr(f"槽位 {sid} 当前 = {slot['value']}{unit}{expired}", f"slot {sid} currently = {slot['value']}{unit}{expired}"))
    return out


def _details(c: Component, links: Optional[dict[str, dict[str, Any]]], *, wavelength_text: Optional[str] = None,
             event: Optional[dict] = None, extra: Sequence[str] = ()) -> str:
    """The parenthesis after a component's name."""
    bits: list[str] = []
    if wavelength_text:
        bits.append(wavelength_text)
    maker = _maker(c)
    if maker:
        bits.append(maker)
    bits += _props_text(c)
    a = c.annotations
    if a.function:
        bits.append(tr(f"作用：{a.function}", f"function: {a.function}"))
    bits.append(tr(f"在 {_pos(c)} mm", f"at {_pos(c)} mm"))
    if event is not None:
        bits.append(tr(f"命中偏离中心 {_f1(event['offset_mm'])} mm", f"hit {_f1(event['offset_mm'])} mm off center"))
        bits.append(tr(f"入射 {_f1(event['incidence_deg'])}°", f"incidence {_f1(event['incidence_deg'])} deg"))
        bits.append(tr(f"累计 {_f1(event['path_mm'])} mm", f"path {_f1(event['path_mm'])} mm"))
    bits += list(extra)
    if a.motorized:
        motor = [tr("电动：是", "motorized: yes")]
        if a.actuator:
            motor.append(a.actuator)
        if a.connection:
            motor.append(tr(f"接 {a.connection}", f"connected to {a.connection}"))
        bits.append(_comma().join(motor))
    elif c.type in SOURCE_TYPES:
        bits.append(tr("电动：否", "motorized: no"))
    bits += _link_text(c, links)
    for pair in a.extra[:6]:
        bits.append(f"{pair.key}: {pair.value}" if pair.value else pair.key)
    if a.notes:
        note = " ".join(a.notes.split())
        bits.append(tr("备注：", "note: ") + (note if len(note) <= 120 else note[:117] + "..."))
    return _paren(bits)


# ── the beam, in order ───────────────────────────────────────────────────────────────────────


def _event_line(c: Component, ev: dict, links: Optional[dict]) -> str:
    action = ev["action"]
    extra: list[str] = []
    if c.type == "dichroic":
        extra.append(tr("反射", "reflects") if action == "reflect" else tr("透射", "transmits"))
    d = _details(c, links, event=ev, extra=extra)
    if action == "blocked":
        why = {"filter": tr("光的波长不在通带内", "the wavelength is outside the passband"),
               "iris": tr("命中点偏离中心超过开口半径", "the hit is beyond the open radius"),
               "shutter": tr("快门关着", "the shutter is closed")}.get(c.type)
        tail = (tr("（", " (") + why + tr("）", ")")) if why else ""
        return tr(f"被 {_name(c)} 挡住{d}{tail}", f"blocked by {_name(c)}{d}{tail}")
    if action == "back_hit":
        return tr(f"打在 {_name(c)} 的背面，被挡住{d}", f"hits the back of {_name(c)} and is stopped{d}")
    if action == "stop":
        return tr(f"终止于 {_name(c)}{d}", f"ends at {_name(c)}{d}")
    if action == "split":
        return f"{_name(c)}{d}" + tr("：分束", ": splits")
    return f"{_name(c)}{d}"


def _branch_lines(layout: Layout, by_id: dict[str, dict], branch: dict, links: Optional[dict], depth: int) -> list[str]:
    pad = "  " * depth
    lines: list[str] = []
    for ev in branch["events"]:
        c = layout.component(ev["component"])
        if c is not None:
            lines.append(f"{pad}→ {_event_line(c, ev, links)}")
    end = branch["end"]
    if end == "left_table":
        last = branch["segments"][-1]
        lines.append(pad + "→ " + tr(f"离开平台（在 ({_g(last['x1'])}, {_g(last['y1'])}) mm 处出界）",
                                      f"leaves the table (out at ({_g(last['x1'])}, {_g(last['y1'])}) mm)"))
    elif end == "max_events":
        lines.append(pad + "→ " + tr("作用次数达到上限（64），可能有回环", "event limit (64) reached; the beam may be looping"))
    elif end == "budget":
        lines.append(pad + "→ " + tr("追迹在这里被截断（整张图的作用次数达到上限），后面的光没有追迹",
                                      "the trace is cut here (the layout's event limit was reached); nothing is traced beyond"))
    elif end == "split":
        for suffix, word in ((".r", tr("反射支", "reflected branch")), (".t", tr("透射支", "transmitted branch"))):
            child = by_id.get(branch["branch"] + suffix)
            if child is None:
                continue
            share = _g(child["fraction"] * 100, 1)
            lines.append(tr(f"{pad}  ↳ {word} {child['branch']}（强度 {share}%）",
                            f"{pad}  ↳ {word} {child['branch']} (intensity {share}%)"))
            lines += _branch_lines(layout, by_id, child, links, depth + 2)
    return lines


def _tree_lines(layout: Layout, tree: dict, number: int, links: Optional[dict],
                continued_from: dict[str, str]) -> list[str]:
    src = layout.component(tree["source"])
    if src is None:
        return []
    head = f"{number}) {_name(src)}{_details(src, links, wavelength_text=_wl(tree['wavelength_nm']))}"
    if src.id in continued_from:
        head += tr(f"；光从 {continued_from[src.id]} 接过来", f"; the light comes in at {continued_from[src.id]}")
    by_id = {b["branch"]: b for b in tree["branches"]}
    return [head] + ["   " + ln for ln in _branch_lines(layout, by_id, tree["branches"][0], links, 0)]


# ── problems and comments ────────────────────────────────────────────────────────────────────


def _cut_text(cut: dict) -> str:
    """The sentence that says the trace was cut: at the top of the text and the first problem."""
    wl = _wl(cut["wavelength_nm"])
    nb, nt = cut["branches"], cut["trees"]
    return tr(f"追迹被截断：整张图的作用次数达到上限（{cut['events']} 次，{cut['branches']} 支，{cut['trees']} 棵光路树），"
              f"停在 {cut['source']} {wl} 的支 {cut['branch']}；后面的光没有追迹，所以「没被照到的器件」「错过口径」"
              f"「光纤没有光进来」这几类结论这里没有给出。多半是分束器、光源或波长太多",
              f"trace cut: the layout reached its limit of {cut['events']} events ({nb} branch{'' if nb == 1 else 'es'}, "
              f"{nt} beam{'' if nt == 1 else 's'}); "
              f"it stops in the beam {cut['source']} {wl}, branch {cut['branch']}, and nothing after that is traced, so the "
              f"conclusions about components no beam reaches, missed apertures and fibers without light are left out here. "
              f"Usually there are too many splitters, sources or wavelengths")


def _problems(layout: Layout, trace: list[dict]) -> list[str]:
    out: list[str] = []
    lit = lit_components(trace)
    sources = [c for c in layout.components if c.type in SOURCE_TYPES]
    targets = [c for c in layout.components if c.type not in SOURCE_TYPES]
    cut = truncation(trace)
    if cut:
        out.append(_cut_text(cut))

    for tree in trace:
        who = f"{tree['source']} {_wl(tree['wavelength_nm'])}"
        for br in tree["branches"]:
            for ev in br["events"]:
                c = layout.component(ev["component"])
                if c is None:
                    continue
                if ev["action"] == "back_hit":
                    out.append(tr(f"打在背面：{who} 的光打在 {_name(c)} 的背面（被挡住，没有继续）",
                                  f"back side: the beam {who} hits the back of {_name(c)} (stopped)"))
                elif ev["action"] in ("reflect", "transmit", "split", "stop") and \
                        abs(ev["offset_mm"]) > OFF_CENTER_SHARE * c.aperture_mm:
                    share = abs(ev["offset_mm"]) / c.aperture_mm * 100
                    out.append(tr(f"命中偏离 > 20% 口径：{who} 在 {_name(c)} 上偏离中心 {_f1(ev['offset_mm'])} mm"
                                  f"（口径 {_g(c.aperture_mm)} mm 的 {share:.0f}%）",
                                  f"off center by more than 20% of the aperture: {who} hits {_name(c)} "
                                  f"{_f1(ev['offset_mm'])} mm from its center ({share:.0f}% of the {_g(c.aperture_mm)} mm aperture)"))
            if br["end"] == "left_table":
                last = br["segments"][-1]
                out.append(tr(f"光线离开平台：{who}（支 {br['branch']}）在 ({_g(last['x1'])}, {_g(last['y1'])}) mm 处出界",
                              f"leaves the table: {who} (branch {br['branch']}) goes out at ({_g(last['x1'])}, {_g(last['y1'])}) mm"))
            elif br["end"] == "max_events":
                out.append(tr(f"作用次数达到上限：{who}（支 {br['branch']}）超过 64 次作用，可能有回环",
                              f"event limit reached: {who} (branch {br['branch']}) passed 64 interactions; it may be looping"))

    if sources and cut:
        pass        # what no beam does cannot be told from a trace that stops short
    elif sources:
        # a beam that passes just outside a component nothing else lights
        seen: set[tuple] = set()
        for m in near_misses(layout, trace):
            c = layout.component(m["component"])
            key = (m["component"], m["source"], m["wavelength_nm"])
            if c is None or c.id in lit or key in seen:
                continue
            seen.add(key)
            out.append(tr(f"错过口径：{_name(c)} 没被任何光照到；{m['source']} {_wl(m['wavelength_nm'])} 的光从它的作用面旁边经过，"
                          f"落点偏出口径边缘 {_f1(m['outside_mm'])} mm（离中心 {_f1(abs(m['offset_mm']))} mm）",
                          f"missed aperture: nothing lights {_name(c)}; the beam {m['source']} {_wl(m['wavelength_nm'])} passes "
                          f"its surface {_f1(m['outside_mm'])} mm beyond the edge of the aperture ({_f1(abs(m['offset_mm']))} mm from the center)"))
        dark = [c for c in targets if c.id not in lit]
        if dark:
            names = ", ".join(c.label or c.id for c in dark[:20]) + (f" ... (+{len(dark) - 20})" if len(dark) > 20 else "")
            out.append(tr(f"没被任何光照到的器件（{len(dark)} 个）：{names}", f"components no beam reaches ({len(dark)}): {names}"))
    else:
        out.append(tr("图里没有光源（激光器、光纤输出、灯、LED）", "the layout has no light source (laser, fiber output, lamp, LED)"))

    continued = {c.props["continues_at"] for c in layout.components if c.type == "fiber_in" and c.props.get("continues_at")}
    for c in layout.components:
        target = c.props.get("continues_at") if c.type == "fiber_in" else None
        if target:
            tc = layout.component(target)
            if tc is None or tc.type != "fiber_out":
                out.append(tr(f"光纤指向错了：{_name(c)} 接到 {target!r}，但图里没有这个光纤输出",
                              f"broken fiber link: {_name(c)} continues at {target!r}, which is not a fiber output of this layout"))
    traced = {t["source"] for t in trace}
    for c in layout.components:
        if c.type == "fiber_out" and c.id in continued and c.id not in traced and not cut:
            out.append(tr(f"光纤输出 {c.label or c.id} 没有光进来（接它的光纤耦合没被光照到）",
                          f"fiber output {c.label or c.id} gets no light (the fiber coupler feeding it is not lit)"))
    return out


def _comment_lines(comments: list[dict]) -> list[str]:
    out: list[str] = []
    for c in comments[:_MAX_COMMENT_LINES]:
        who = tr("操作员", "operator") if c.get("author") == "operator" else "agent"
        where = f" · {c['component_id']}" if c.get("component_id") else ""
        text = " ".join(str(c.get("text") or "").split())
        text = text if len(text) <= 300 else text[:297] + "..."
        out.append(f"- {c.get('id')} [{who} {c.get('by')}{where}] {text}")
    if len(comments) > _MAX_COMMENT_LINES:
        out.append(tr(f"- … 另有 {len(comments) - _MAX_COMMENT_LINES} 条", f"- ... {len(comments) - _MAX_COMMENT_LINES} more"))
    return out


def _component_list(layout: Layout, links: Optional[dict]) -> list[str]:
    out = []
    for c in layout.components:
        wl = _source_wavelengths(c) if c.type in SOURCE_TYPES else None
        facing = tr(f"朝向 {_g(c.angle_deg)}°", f"facing {_g(c.angle_deg)} deg")
        out.append(f"- {_name(c)}{_details(c, links, wavelength_text=wl, extra=[facing])}")
    return out


# ── the whole text ───────────────────────────────────────────────────────────────────────────


def summarize(layout: Any, trace: Optional[list[dict]] = None, *, links: Optional[dict[str, dict[str, Any]]] = None,
              comments: Optional[list[dict]] = None, lang: Optional[str] = None, limit: int = MAX_CHARS) -> str:
    """The description of *layout* (a :class:`Layout` or its dict). *trace* is computed when not
    given; *links* is :func:`mast.optical_layout.links.links_state` (without it only the
    references are named); *comments* are the unresolved ones. ``lang`` ``"zh"`` / ``"en"``
    overrides the active language."""
    lay = normalize_layout(layout)
    with use_lang(lang):
        return _summarize(lay, trace if trace is not None else trace_layout(lay), links, comments or [], limit)


def _summarize(lay: Layout, trace: list[dict], links: Optional[dict], comments: list[dict], limit: int) -> str:
    sources = [c for c in lay.components if c.type in SOURCE_TYPES]
    t = lay.table
    title = lay.title or lay.id
    stamp = lay.updated_at[:16].replace("T", " ") + " UTC" if lay.updated_at else ""
    saved = ""
    if lay.updated_by or stamp:
        saved = tr(f"；最近一次保存：{lay.updated_by} {stamp}".rstrip(), f"; last saved by {lay.updated_by} {stamp}".rstrip())
    lines = [
        tr(f"光路图「{title}」（id {lay.id}，第 {lay.revision} 版）", f'Optical layout "{title}" (id {lay.id}, revision {lay.revision})'),
        tr(f"平台 {_g(t.width_mm)} × {_g(t.height_mm)} mm，孔距 {_g(t.hole_pitch_mm)} mm（{_grid(t.grid)}）；"
           f"器件 {len(lay.components)} 个，其中光源 {len(sources)} 个{saved}",
           f"Table {_g(t.width_mm)} x {_g(t.height_mm)} mm, hole pitch {_g(t.hole_pitch_mm)} mm ({_grid(t.grid)}); "
           f"{len(lay.components)} components, {len(sources)} light source{'' if len(sources) == 1 else 's'}{saved}"),
        tr("坐标：平台左下角为原点，x 向右，y 向上，单位 mm；角度从 +x 逆时针量。",
           "Coordinates: origin at the table's lower left, x to the right, y up, in mm; angles counter-clockwise from +x."),
    ]
    cut = truncation(trace)
    if cut:         # at the top as well as the first problem: the text is cut at MAX_CHARS, from the end
        lines.append(_cut_text(cut))
    if lay.notes:
        note = " ".join(lay.notes.split())
        lines.append(tr("备注：", "Note: ") + (note if len(note) <= 400 else note[:397] + "..."))
    lines.append("")

    if sources:
        lines.append(tr("光路（沿光束的顺序）：", "Beam paths (in the order along the beam):"))
        continued_from: dict[str, str] = {}
        for tree in trace:
            for br in tree["branches"]:
                if br["end"] == "stop" and br["events"]:
                    last = lay.component(br["events"][-1]["component"])
                    if last is not None and last.type == "fiber_in" and last.props.get("continues_at"):
                        continued_from.setdefault(last.props["continues_at"], last.label or last.id)
        for n, tree in enumerate(trace, 1):
            lines += _tree_lines(lay, tree, n, links, continued_from)
        if not trace:
            lines.append(tr("（没有光线：光源都是被某个光纤接过来的，而它们没被照到）", "(no rays: every source is fed by a fiber that no light reaches)"))
    else:
        lines.append(tr("图里还没有光源，下面直接列出器件和注释：", "The layout has no light source yet; its components and annotations:"))
        lines += _component_list(lay, links) or [tr("（没有器件）", "(no components)")]
    lines.append("")

    problems = _problems(lay, trace)
    if problems:
        lines.append(tr(f"需要注意（{len(problems)} 条）：", f"Things to look at ({len(problems)}):"))
        lines += [f"- {p}" for p in problems[:_MAX_PROBLEM_LINES]]
        if len(problems) > _MAX_PROBLEM_LINES:
            lines.append(tr(f"- … 另有 {len(problems) - _MAX_PROBLEM_LINES} 条", f"- ... {len(problems) - _MAX_PROBLEM_LINES} more"))
    else:
        lines.append(tr("需要注意：没有发现问题。", "Things to look at: none found."))
    if comments:
        lines.append("")
        lines.append(tr(f"未解决的评论（{len(comments)} 条）：", f"Unresolved comments ({len(comments)}):"))
        lines += _comment_lines(comments)
    text = "\n".join(lines).rstrip() + "\n"
    if len(text) > limit:
        tail = tr(f"\n…（摘要在 {limit} 字符处截断）", f"\n... (summary cut at {limit} characters)")
        text = text[:max(0, limit - len(tail))] + tail
    return text


__all__ = ["MAX_CHARS", "OFF_CENTER_SHARE", "summarize"]
