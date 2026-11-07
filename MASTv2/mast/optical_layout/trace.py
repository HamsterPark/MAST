"""Two-dimensional ray trace of an optical layout (top view).

Contract: ``docs/v2/design/optical_layout.md`` §4. The front end has its own TypeScript copy of
this algorithm; both are run against the same fixtures (``tests/fixtures/optical_layout/*.json``,
described in the README next to them) and must agree. Keep this file plain on purpose - no
vectorisation, no library geometry - so that the TypeScript port can follow it line by line.

Geometry
--------
A component has a centre C and a local frame whose +x axis points along ``angle_deg``:
``n = (cos a, sin a)`` (the "normal") and ``u = (-sin a, cos a)`` (local +y, along the working
surface). The working surface is the segment ``C +- u * aperture / 2``. ``deg -> rad`` is
``deg * (pi / 180)`` (the same expression in both languages).

A ray is ``P + t * d`` with ``|d| = 1``. It meets a surface at ``t = ((C - P) . n) / (d . n)``
(no meeting when ``|d . n| < 1e-9``: parallel). The meeting is a *hit* when ``t > 1e-6``, the
distance from C along ``u`` (``offset``) satisfies ``|offset| <= aperture / 2 + 1e-9``, and, for
the component the ray has just acted on, ``t >= 1e-3`` (a ray leaving a surface must not hit it
again). The nearest hit wins. Hits whose ``t`` differ by no more than ``1e-9`` mm are a *tie* and the
component earlier in the list wins: a later candidate replaces the best so far only when
``t < best - 1e-9``. (Two components whose working surfaces are collinear and overlap are hit at
the same point; their ``t`` differ in the last bit of a double, and which one is "nearer" would be
rounding noise - different in Python and in the front end's JavaScript, which do not even agree on
``sin(45 deg)`` to the last bit. The tolerance is absolute: ``t`` never exceeds 10 000 mm, where
the rounding noise of a double is about 1e-11 mm, and surfaces that do not coincide are at least
the 0.01 mm of the coordinate grid apart, so noise and real differences are far apart at every
scale.) Sources are never hit.

What happens at a hit (``front`` means ``d . n < 0``):

======================  =====================================================================
mirror, grating,        front: ``reflect`` (``d' = d - 2 (d . n) n``); back: ``back_hit``,
sample (reflects),      the ray ends.
custom (reflect)
beam_splitter           ``split``: the branch ends (``split``); a reflected child ``.r``
                        (fraction * R, ``d'`` as above) and a transmitted child ``.t``
                        (fraction * (1 - R)) continue from the hit; a child below 1e-3 is not
                        created. ``R = props.reflect_fraction``, default 0.5. Both faces.
dichroic                ``reflect`` or ``transmit`` by wavelength: ``reflects: short`` (the
                        default) reflects ``wavelength < edge_nm``, ``long`` reflects
                        ``wavelength > edge_nm``; equal to the edge transmits; a ray of unknown
                        wavelength transmits. Both faces.
lens, objective,        ``transmit`` (no deviation, no loss), both faces.
waveplate, polarizer,
window, nd, aotf,
prism, periscope,
custom (transmit)
filter                  ``transmit`` when ``pass_min_nm <= wavelength <= pass_max_nm`` (a missing
                        bound is open; unknown wavelength passes), else ``blocked``: the ray ends.
iris                    ``blocked`` when ``props.open_mm`` is set and ``|offset| > open_mm / 2 +
                        1e-9``, else ``transmit``.
shutter                 ``blocked`` when ``props.closed``, else ``transmit``.
detector, camera,       front: ``stop``; back: ``back_hit``; either way the ray ends.
spectrometer,
power_meter,
beam_dump, blocker,
fiber_in, sample (does
not reflect), custom
(stop)
======================  =====================================================================

Every hit is one *event* (``action`` is one of ``reflect``, ``transmit``, ``split``, ``stop``,
``blocked``, ``back_hit``) and counts towards the 64 of a ray's lineage: after the 64th event
that did not end the branch the branch ends with ``max_events`` (no segment after it).

A ray that finds no hit before it leaves the table rectangle expanded by 1 m on every side, or
before it has travelled 10 m, ends with ``left_table`` at that point (the last segment runs to
it). A ray that starts outside the rectangle ends at once, with a zero-length segment.

Output
------
One tree per ray source and wavelength, in this order: the sources in layout order (a laser or
fibre output with ``wavelengths_nm`` gives one tree per wavelength, in the order listed), then the
continuations of fibres as they are met. A branch is ``{"branch", "fraction", "segments",
"events", "end"}``; branches are listed depth first, parent before children, the reflected child
before the transmitted one. ``segments[i]`` runs from the previous point of the branch (the
source, or the point the parent split at) to ``events[i]``; a branch that ends with
``left_table`` has one more, last segment, to the point where it left, and one that ends with
``budget`` has one more to the point of the hit it could not record. Branch ids: the root is
``"0"``, a split appends ``".r"`` / ``".t"``. ``end`` is ``stop`` (a terminal component),
``blocked``, ``back_hit``, ``split``, ``left_table``, ``max_events`` or ``budget`` (the whole
trace ran out of events, see Budget). All lengths and coordinates are rounded to 0.001, angles to
0.001 degrees, fractions to 1e-6; ``-0.0`` is ``0.0``.

Fibres: a ``fiber_in`` whose ``props.continues_at`` names a ``fiber_out`` of the layout passes
the light on - when a ray *stops* at its front, that ``fiber_out`` becomes a source of the same
wavelength, with fraction 1.0 (a new tree). A ``fiber_out`` named by some ``continues_at`` emits
only that way, never from its own props; one tree per (fibre output, wavelength), so a fibre
loop ends.

Budget
------
The 64 events and the fraction floor bound one lineage, not the trace: a layout that splits its
light again and again (many beam splitters, many sources, many wavelengths) still makes a trace
of any size, and with it everything made from the trace. So the whole trace - all trees - records
at most :data:`MAX_TRACE_EVENTS` events. They are counted in the order the trace is produced (the
trees in the order above, inside a tree depth first with the reflected child before the transmitted
one, inside a branch one after the other), and the hit that would be event number
``MAX_TRACE_EVENTS + 1`` is not recorded: its branch gets the segment to the point where the hit
would have been and ends with ``budget`` (one more segment than events, like ``left_table``), and
the trace stops there - the branches and trees that would have followed are not traced. The branch
that ends with ``budget`` is therefore the last branch of the last tree, and a trace that needs
exactly :data:`MAX_TRACE_EVENTS` events is complete. A split makes at most two branches, so the same
number bounds the branches (there is no second limit); :func:`truncation` reads the notice off a
trace, from the trace alone.

:func:`near_misses` is separate from the trace: it reports where a ray crossed the line of a
component's working surface just outside the aperture.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any, Optional

from mast.optical_layout.model import TYPES, Component, Layout, normalize_layout

#: a hit needs t above this (mm)
EPS_T = 1e-6
#: the component a ray has just acted on is ignored below this (mm)
EPS_SELF = 1e-3
#: tolerance on the aperture test (mm)
EPS_APERTURE = 1e-9
#: two hits whose t differ by no more than this (mm) are a tie: the component earlier in the list wins
EPS_TIE = 1e-9
#: |d . n| below this is "parallel"
EPS_PARALLEL = 1e-9
#: events in one ray's lineage
MAX_EVENTS = 64
#: events one whole trace (all trees, all branches) records; the hit after the last one ends the
#: trace with ``budget``
MAX_TRACE_EVENTS = 5000
#: a branch below this fraction is not followed
MIN_FRACTION = 1e-3
#: the rectangle a ray may roam is the table expanded by this much on every side (mm)
TABLE_MARGIN_MM = 1000.0
#: a single segment never runs longer than this (mm)
MAX_SEGMENT_MM = 10000.0
#: a near miss is a crossing outside the aperture but within this many apertures of the centre
NEAR_MISS_APERTURES = 1.0

_DEG = math.pi / 180.0
_INF = math.inf


def _r3(v: float) -> float:
    r = round(v, 3)
    return r + 0.0          # -0.0 -> 0.0


def _r6(v: float) -> float:
    r = round(v, 6)
    return r + 0.0


class _Comp:
    """A component prepared for tracing."""

    __slots__ = ("index", "id", "type", "role", "cx", "cy", "nx", "ny", "half", "props",
                 "action_kind")

    def __init__(self, index: int, c: Component) -> None:
        a = c.angle_deg * _DEG
        self.index = index
        self.id = c.id
        self.type = c.type
        self.role = TYPES[c.type].role
        self.cx = c.x_mm
        self.cy = c.y_mm
        self.nx = math.cos(a)
        self.ny = math.sin(a)
        self.half = c.aperture_mm / 2.0
        self.props = c.props
        self.action_kind = self._kind()

    def _kind(self) -> str:
        """``source`` | ``reflect`` | ``split`` | ``dichroic`` | ``transmit`` | ``filter`` | ``iris`` |
        ``shutter`` | ``stop``."""
        role = self.role
        if role == "mirror":
            return "reflect"
        if role == "splitter":
            return "split"
        if role in ("dichroic", "transmit", "filter", "iris", "shutter", "source"):
            return role
        if role == "terminal":
            return "stop"
        if role == "sample":
            return "reflect" if self.props.get("reflects") else "stop"
        # custom
        return {"reflect": "reflect", "stop": "stop"}.get(self.props.get("behavior"), "transmit")


class _World:
    def __init__(self, layout: Layout) -> None:
        self.comps = [_Comp(i, c) for i, c in enumerate(layout.components)]
        self.targets = [c for c in self.comps if c.action_kind != "source"]
        self.by_id = {c.id: c for c in self.comps}
        t = layout.table
        self.xmin = -TABLE_MARGIN_MM
        self.xmax = t.width_mm + TABLE_MARGIN_MM
        self.ymin = -TABLE_MARGIN_MM
        self.ymax = t.height_mm + TABLE_MARGIN_MM

    def distance_to_edge(self, px: float, py: float, dx: float, dy: float) -> float:
        """How far the ray can run before it leaves the rectangle (0 when it starts outside)."""
        if not (self.xmin <= px <= self.xmax and self.ymin <= py <= self.ymax):
            return 0.0
        tx = _INF
        if dx > 0:
            tx = (self.xmax - px) / dx
        elif dx < 0:
            tx = (self.xmin - px) / dx
        ty = _INF
        if dy > 0:
            ty = (self.ymax - py) / dy
        elif dy < 0:
            ty = (self.ymin - py) / dy
        return min(tx, ty)

    def nearest_hit(self, px: float, py: float, dx: float, dy: float,
                    last: Optional[_Comp]) -> Optional[tuple]:
        """``(t, comp, x, y, offset, d.n)`` of the nearest hit, or ``None``."""
        best: Optional[tuple] = None
        best_t = _INF
        for c in self.targets:
            dn = dx * c.nx + dy * c.ny
            if abs(dn) < EPS_PARALLEL:
                continue
            t = ((c.cx - px) * c.nx + (c.cy - py) * c.ny) / dn
            if t <= EPS_T:
                continue
            if c is last and t < EPS_SELF:
                continue
            hx = px + t * dx
            hy = py + t * dy
            offset = (hx - c.cx) * (-c.ny) + (hy - c.cy) * c.nx
            if abs(offset) > c.half + EPS_APERTURE:
                continue
            if t < best_t - EPS_TIE:            # a later component must be nearer by more than the tolerance
                best_t = t
                best = (t, c, hx, hy, offset, dn)
        return best


def _wavelengths(c: _Comp) -> list[Optional[float]]:
    p = c.props
    if p.get("wavelengths_nm"):
        return list(p["wavelengths_nm"])
    if p.get("wavelength_nm") is not None:
        return [p["wavelength_nm"]]
    return [None]


def _interact(c: _Comp, dn: float, offset: float, wavelength: Optional[float]) -> tuple[str, Optional[str]]:
    """``(action, end)``: what the hit does, and how the branch ends (``None`` = it goes on;
    ``split`` is handled by the caller)."""
    kind = c.action_kind
    front = dn < 0
    if kind == "reflect":
        return ("reflect", None) if front else ("back_hit", "back_hit")
    if kind == "stop":
        return ("stop", "stop") if front else ("back_hit", "back_hit")
    if kind == "split":
        return "split", "split"
    if kind == "dichroic":
        edge = c.props.get("edge_nm")
        if wavelength is None or edge is None:
            return "transmit", None
        if c.props.get("reflects", "short") == "short":
            reflected = wavelength < edge
        else:
            reflected = wavelength > edge
        return ("reflect", None) if reflected else ("transmit", None)
    if kind == "filter":
        lo, hi = c.props.get("pass_min_nm"), c.props.get("pass_max_nm")
        if wavelength is not None and ((lo is not None and wavelength < lo)
                                       or (hi is not None and wavelength > hi)):
            return "blocked", "blocked"
        return "transmit", None
    if kind == "iris":
        open_mm = c.props.get("open_mm")
        if open_mm is not None and abs(offset) > open_mm / 2.0 + EPS_APERTURE:
            return "blocked", "blocked"
        return "transmit", None
    if kind == "shutter":
        return ("blocked", "blocked") if c.props.get("closed") else ("transmit", None)
    return "transmit", None


class _Budget:
    """What the whole trace may still record (see Budget in the module docstring)."""

    __slots__ = ("limit", "events", "cut")

    def __init__(self, limit: int) -> None:
        self.limit = limit      # events the trace may record in all
        self.events = 0         # events recorded so far, in every tree
        self.cut = False        # a hit found no room: the trace stops


def _walk(world: _World, budget: _Budget, out: list, bid: str, px: float, py: float, dx: float,
          dy: float, fraction: float, path: float, n_events: int, last: Optional[_Comp],
          wavelength: Optional[float]) -> None:
    branch: dict[str, Any] = {"branch": bid, "fraction": _r6(fraction), "segments": [],
                              "events": [], "end": ""}
    out.append(branch)
    while True:
        limit = min(world.distance_to_edge(px, py, dx, dy), MAX_SEGMENT_MM)
        hit = world.nearest_hit(px, py, dx, dy, last)
        if hit is None or hit[0] > limit:
            branch["segments"].append(_segment(px, py, px + limit * dx, py + limit * dy))
            branch["end"] = "left_table"
            return
        t, c, hx, hy, offset, dn = hit
        path += t
        branch["segments"].append(_segment(px, py, hx, hy))
        if budget.events >= budget.limit:       # no room for this hit: not recorded, the trace stops
            branch["end"] = "budget"
            budget.cut = True
            return
        budget.events += 1
        action, end = _interact(c, dn, offset, wavelength)
        branch["events"].append({
            "component": c.id, "action": action, "x": _r3(hx), "y": _r3(hy),
            "offset_mm": _r3(offset),
            "incidence_deg": _r3(math.acos(min(1.0, abs(dn))) * (180.0 / math.pi)),
            "path_mm": _r3(path)})
        n_events += 1
        last = c
        px, py = hx, hy
        reflected = (dx - 2.0 * dn * c.nx, dy - 2.0 * dn * c.ny)
        if action == "split":
            if n_events >= MAX_EVENTS:
                branch["end"] = "max_events"
                return
            branch["end"] = "split"
            r = c.props.get("reflect_fraction", 0.5)
            for suffix, f, direction in ((".r", fraction * r, reflected),
                                         (".t", fraction * (1.0 - r), (dx, dy))):
                if f < MIN_FRACTION:
                    continue
                _walk(world, budget, out, bid + suffix, px, py, direction[0], direction[1], f, path,
                      n_events, last, wavelength)
                if budget.cut:
                    return
            return
        if end is not None:
            branch["end"] = end
            return
        if action == "reflect":
            dx, dy = reflected
        if n_events >= MAX_EVENTS:
            branch["end"] = "max_events"
            return


def _segment(x0: float, y0: float, x1: float, y1: float) -> dict[str, float]:
    return {"x0": _r3(x0), "y0": _r3(y0), "x1": _r3(x1), "y1": _r3(y1)}


def trace_layout(layout: Any, *, max_trace_events: Optional[int] = None) -> list[dict[str, Any]]:
    """The trace of *layout* (a :class:`Layout` or the dict a client / file gives): see the
    module docstring for the rules and the output. *max_trace_events* is the budget of the whole
    trace (default :data:`MAX_TRACE_EVENTS`); give a smaller one to get a cut trace of a small
    layout, as the shared ``trace_budget_cut`` fixture does."""
    lay = normalize_layout(layout)
    world = _World(lay)
    budget = _Budget(MAX_TRACE_EVENTS if max_trace_events is None else max(0, int(max_trace_events)))
    continued = {c.props["continues_at"] for c in world.comps
                 if c.type == "fiber_in" and c.props.get("continues_at")}
    queue: deque = deque()
    seen: set = set()
    for c in world.comps:
        if c.action_kind != "source" or (c.type == "fiber_out" and c.id in continued):
            continue
        for wl in _wavelengths(c):
            queue.append((c, wl))
            seen.add((c.id, wl))
    trees: list[dict[str, Any]] = []
    while queue:
        src, wl = queue.popleft()
        tree: dict[str, Any] = {"source": src.id, "wavelength_nm": wl, "branches": []}
        _walk(world, budget, tree["branches"], "0", src.cx, src.cy, src.nx, src.ny, 1.0, 0.0, 0, None, wl)
        trees.append(tree)
        if budget.cut:
            break
        for br in tree["branches"]:
            if br["end"] != "stop" or not br["events"]:
                continue
            last = world.by_id.get(br["events"][-1]["component"])
            if last is None or last.type != "fiber_in":
                continue
            nxt = world.by_id.get(last.props.get("continues_at", ""))
            if nxt is None or nxt.type != "fiber_out" or (nxt.id, wl) in seen:
                continue
            seen.add((nxt.id, wl))
            queue.append((nxt, wl))
    return trees


def truncation(trace: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """``None`` for a complete trace. For one the budget cut: what was traced and where it stopped,
    ``{"reason": "events", "events": n, "branches": m, "trees": k, "source", "wavelength_nm",
    "branch"}`` - n events in m branches of k trees, the last of them the tree of ``source`` at
    ``wavelength_nm``, cut in ``branch``. Derived from the trace alone (a branch that ends with
    ``budget``), so it survives a trace that went through JSON."""
    for tree in trace:
        for br in tree["branches"]:
            if br["end"] == "budget":
                return {"reason": "events",
                        "events": sum(len(b["events"]) for t in trace for b in t["branches"]),
                        "branches": sum(len(t["branches"]) for t in trace), "trees": len(trace),
                        "source": tree["source"], "wavelength_nm": tree["wavelength_nm"],
                        "branch": br["branch"]}
    return None


def lit_components(trace: list[dict[str, Any]]) -> set[str]:
    """Ids of the components some ray acted on (hit)."""
    return {e["component"] for tree in trace for br in tree["branches"] for e in br["events"]}


def near_misses(layout: Any, trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Where a ray crossed the *line* of a component's working surface just outside the aperture.

    For every segment of the trace and every component that is not a source: the segment crosses
    the surface's line at parameter ``t`` with ``1e-3 < t < length - 1e-6``, the crossing lies
    outside the aperture (``|offset| > aperture / 2 + 1e-9``) but within one aperture of the
    centre (``|offset| <= aperture``). Result items: ``{"component", "source",
    "wavelength_nm", "branch", "x", "y", "offset_mm", "outside_mm"}`` (``outside_mm`` = how far
    beyond the edge), in trace order. Components that other rays do hit are not excluded here;
    the summary decides what is worth saying.
    """
    lay = normalize_layout(layout)
    comps = [_Comp(i, c) for i, c in enumerate(lay.components)]
    targets = [c for c in comps if c.action_kind != "source"]
    out: list[dict[str, Any]] = []
    for tree in trace:
        for br in tree["branches"]:
            for seg in br["segments"]:
                sx, sy = seg["x0"], seg["y0"]
                ex, ey = seg["x1"], seg["y1"]
                length = math.hypot(ex - sx, ey - sy)
                if length <= EPS_SELF:
                    continue
                dx, dy = (ex - sx) / length, (ey - sy) / length
                for c in targets:
                    dn = dx * c.nx + dy * c.ny
                    if abs(dn) < EPS_PARALLEL:
                        continue
                    t = ((c.cx - sx) * c.nx + (c.cy - sy) * c.ny) / dn
                    if not (EPS_SELF < t < length - EPS_T):
                        continue
                    hx, hy = sx + t * dx, sy + t * dy
                    offset = (hx - c.cx) * (-c.ny) + (hy - c.cy) * c.nx
                    outside = abs(offset) - c.half
                    if outside <= EPS_APERTURE or abs(offset) > c.half * 2.0 * NEAR_MISS_APERTURES + EPS_APERTURE:
                        continue
                    out.append({"component": c.id, "source": tree["source"],
                                "wavelength_nm": tree["wavelength_nm"], "branch": br["branch"],
                                "x": _r3(hx), "y": _r3(hy), "offset_mm": _r3(offset),
                                "outside_mm": _r3(outside)})
    return out


__all__ = [
    "EPS_APERTURE", "EPS_PARALLEL", "EPS_SELF", "EPS_T", "EPS_TIE", "MAX_EVENTS", "MAX_SEGMENT_MM",
    "MAX_TRACE_EVENTS", "MIN_FRACTION", "NEAR_MISS_APERTURES", "TABLE_MARGIN_MM", "lit_components",
    "near_misses", "trace_layout", "truncation",
]
