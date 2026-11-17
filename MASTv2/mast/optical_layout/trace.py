"""Ray trace of an optical layout (a top view with heights), with the state of the beam.

Contract: ``docs/v2/design/optical_layout.md`` §4 and §11 (the geometry),
``docs/v2/design/optical_layout_v2.md`` §4 and §12 (the beam: power, polarization, Gaussian beam) and
``docs/v2/design/optical_layout_3d.md`` §3 and §12 (heights: see Heights below).
The front end has its own TypeScript copy of this algorithm; both are run against the same
fixtures (``tests/fixtures/optical_layout/*.json``, described in the README next to them) and must
agree. Keep this file plain on purpose - no vectorisation, no library geometry, no complex numbers
- so that the TypeScript port can follow it line by line.

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
pbs                     like ``beam_splitter``, but the two fractions follow the polarization
                        of the arriving beam (see Beam): ``fraction * S_r0 / S0`` and
                        ``fraction * S_t0 / S0``, 0.5 each when no light arrives.
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
                        1e-9`` or ``|dz| > open_mm / 2 + 1e-9`` (see Heights), else ``transmit``.
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

Every tree, segment and event also has a ``beam`` (see Beam): a tree
``{"source_mw", "m2"}``, a segment the state at its *start* ``{"p", "s", "q"}``, an event the
state on *arrival*, before the component acts ``{"p", "mw", "d_mm", "s"}``. The geometric keys are
exactly those of the trace without the beam.

Every segment also has its height ``z_mm`` and the components it passes over or under, ``passes``
(``[{"component", "x", "y", "dz_mm"}]``), every event the height of the beam on arrival ``z_mm`` and
how far that is from the component's axis, ``dz_mm`` (see Heights). In order, a segment is
``{x0, y0, x1, y1, z_mm, beam, passes}`` and an event ``{component, action, x, y, z_mm, offset_mm,
dz_mm, incidence_deg, path_mm, beam}``: without the keys of the heights, exactly the trace of v2.

Fibres: a ``fiber_in`` whose ``props.continues_at`` names a ``fiber_out`` of the layout passes
the light on - when a ray *stops* at its front, that ``fiber_out`` becomes a source of the same
wavelength, with fraction 1.0 (a new tree). A ``fiber_out`` named by some ``continues_at`` emits
only that way, never from its own props; one tree per (fibre output, wavelength), so a fibre
loop ends.

Beam
----
A ray carries a Stokes vector ``S = (S0, S1, S2, S3)`` (``S0`` is the power relative to the
tree's source, 1 there) and the complex beam parameter of a Gaussian beam ``q = z + i zR`` in mm,
held as the pair ``(z, zR)``, or ``None`` (no Gaussian beam). The transverse frame of a ray along
``d`` is ``e_h = z x d = (-dy, dx)`` (in the table plane, to the left of the beam) and ``e_v = z``
(up); every polarization angle is measured from ``e_h`` towards ``e_v``: 0 = in the plane of the
table (p for any reflection in it), 90 = vertical (s). ``S1 = I_h - I_v``, ``S2 = I_+45 - I_-45``.

At the source: ``S = (1, cos 2a, sin 2a, 0)`` for ``polarization: linear`` (``a`` =
``pol_angle_deg``, default 0), else ``(1, 0, 0, 0)`` (unpolarized, also for lamps and LEDs).
``q = (0, zR)`` with ``zR = pi w0^2 / (M2 lambda)``, ``w0 = beam_diameter_mm / 2``,
``lambda = wavelength_nm * 1e-6`` mm, ``M2 = props.m2`` (default 1); ``None`` without
``beam_diameter_mm`` or without a wavelength. The tree's ``source_mw`` is ``props.power_mw`` (each
wavelength of a source gets all of it), ``None`` when not given. A tree that continues a fibre
starts again at ``S0 = 1`` with the polarization and beam of that ``fiber_out``'s own props; its
``source_mw`` is the absolute power that reached the ``fiber_in`` on the *first* branch that
continued it (``source_mw * S0`` there, unrounded) times ``props.coupling`` of that ``fiber_in``
(default 1), ``None`` when the incoming absolute power is unknown.

Along a segment of length ``L``: ``S`` stays, ``q -> (z + L, zR)``. The 1/e^2 diameter is
``d(q) = 2 sqrt(lambda M2 (z^2 + zR^2) / (pi zR))``.

At a hit (``T`` = ``props.throughput``, default 1; ``R_m = diag(1, 1, -1, -1)``, any reflection
in the table plane):

======================  =====================================================================
reflect                 ``T R_m S`` (a dichroic has no ``throughput``: ``R_m S``)
lens, objective         ``T S``; ``1/q' = 1/q - 1/f`` when ``focal_mm`` is given and not 0
waveplate               ``T W(theta_eff, delta) S``; ``half``: ``(cos d, sin d) = (-1, 0)``,
                        ``quarter``: ``(0, 1)`` (exact constants); no ``retardance``: ``T S``
polarizer               ``T P(theta_eff) S``, computed as ``k (1, c, s, 0)`` with
                        ``k = (S0 + c S1 + s S2) / 2``, ``(c, s) = (cos, sin)(2 theta_eff)``
nd                      ``10^-od S`` (no ``od``: ``S``)
other transmissions     ``T S`` (dichroic, iris and shutter have no ``throughput``: ``S``)
beam_splitter           ``.r``: ``R R_m S``; ``.t``: ``(1 - R) S``
pbs                     ``.t``: ``P(0) S = k_t (1, 1, 0, 0)``, ``k_t = (S0 + S1) / 2``;
                        ``.r``: ``R_m P(90) S = k_r (1, -1, 0, 0)``, ``k_r = (S0 - S1) / 2``
stop, blocked,          the ray ends; the event holds the arriving state
back_hit
======================  =====================================================================

``W(theta, delta)`` is the Mueller matrix of a retarder with its fast axis at ``theta`` (contract
§4.5). The axis of a waveplate (``fast_axis_deg``) or a polarizer (``axis_deg``, default 0) is
given in the part's own frame, from its local +y (0, in the table plane) towards up (90); the ray
sees it at ``theta_eff = atan2(sin theta * (d . n), cos theta)`` (front: theta; from behind:
-theta, the same axis as 180 - theta). Oblique, this is the physics of the usual parts (contract
§4.5): an absorbing polarizer (wire grid, dichroic sheet) passes the field perpendicular to its
absorbing axis ``a`` and to the ray, ``d x a``; a waveplate of a positive uniaxial crystal (quartz,
MgF2) has its fast, ordinary wave along ``d x c``, ``c`` the optic axis - both ``a`` and ``c`` lie in
the part's face, perpendicular to the axis given. (A negative crystal such as calcite turns the other
way at oblique incidence; the change of retardance with the angle is not modelled.) A polarizing beam
splitter's p and s are fixed by the plane of incidence (the table): 0 and 90 degrees in the ray's own
frame, no projection.

Dark is exact: at a polarizer and at each output of a polarizing beam splitter, an output whose
``k`` is not above :data:`EPS_DARK` times the arriving ``S0`` (this takes in the floating-point
residue of crossed polarizers, about 1e-16, and a negative ``k``) is exactly ``(0, 0, 0, 0)``.
Light of power 0 is traced like any other (its geometry does not depend on the power).

Output (rounded to 1e-9, except ``mw`` and ``source_mw``, to :data:`MW_DECIMALS` decimals, and the
two numbers of ``q``, to 9 *significant* digits - a legal but extreme Rayleigh range can be far below
1e-9 mm and must not become 0; ``-0.0`` is ``0.0``): ``p = S0``; ``s = [S1/S0, S2/S0, S3/S0]``,
``None`` when ``S0 == 0``; ``q = [z, zR]`` or ``None``; ``mw = source_mw * S0`` or ``None``; ``d_mm = d(q)`` on arrival or ``None``; a tree's
``m2`` is ``None`` when it has no Gaussian beam. The tree's ``fraction`` is untouched by all this
(the product of the splitting ratios, as before), except at a ``pbs``.

Heights
-------
The layout is a top view, and every ray stays level: a ray has a height ``z`` (mm above the table
top) that only a periscope changes. A source emits at its own height (``props.z_mm``, else the table's
beam height H = ``table.beam_height_mm``, 75 mm when not given; a fibre continuation at the height of
its ``fiber_out``); a component's optical axis is at ``props.z_mm``, else H. Heights are stored at
0.01 mm and only added, subtracted and compared here - no trigonometry - so both implementations get
the same bits.

A meeting in the plane (above: ``t``, the aperture test on ``offset``) is a hit only if the ray's height
is within half the aperture of the component's axis: ``dz = z - z_port``, ``|dz| <= aperture / 2 +
1e-9`` (the working surface counts as a square, aperture x aperture). Otherwise the ray passes over
(``dz > 0``) or under it and goes on as if it were not there; the nearest hit is looked for among the
meetings that pass the height test only. ``z_port`` is the height of the component's axis, for a
periscope the height of its entrance for this direction:

* a periscope has two heights, ``z_in`` = its ``z_mm`` (else H) and ``z_out`` = its ``z_out_mm``
  (else ``z_in``). Light that travels along its ``angle_deg`` (``d . n > 0``) enters at ``z_in`` and
  leaves at ``z_out``; light the other way round enters at ``z_out`` and leaves at ``z_in``. After it
  (a ``transmit``), the ray's height is ``z + (z_exit - z_entry)`` - two 45 degree mirrors move the
  beam as a whole, so a beam 3 mm above the entrance leaves 3 mm above the exit - and the vertical run
  ``L_v = |z_exit - z_entry|`` is part of the path: it is added to ``path_mm`` (from the next event on)
  and to ``q`` (free propagation). The vertical run is not a segment: it is at the periscope's event,
  from the event's ``z_mm`` to the next segment's.
* an iris with ``open_mm`` blocks in height as it does in the plane: ``|dz| > open_mm / 2 + 1e-9``.

The meetings a segment leaves out because of their height are its ``passes``: those before the hit
that ends it (``t < t_hit - 1e-9``: a component at the very point of the hit, such as one stacked
above the mirror that is hit, is not passed), or, on a segment that runs out of the table or of its
10 m, those up to its end (``t <= length``); in the order of ``t``, where meetings within ``1e-9`` of
the first of a run are at one point and go by component id (the same tolerance as a tie of hits:
their ``t`` differ in the last bit, which Python and JavaScript do not compute alike). A pass is not
an event: it counts towards neither limit, and it does not light the component. The whole trace records at most :data:`MAX_TRACE_PASSES` of them, in the
order the trace is made; after that a segment records none (its geometry does not change).

Output: ``z_mm`` of a segment and of an event, ``dz_mm`` of an event and the ``x``, ``y``, ``dz_mm`` of
a pass are rounded to 0.001 (``-0.0`` is ``0.0``), like the rest of the geometry.

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
component's working surface just outside the aperture, at the component's height.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any, Optional

from mast.optical_layout.model import (
    TYPES,
    Component,
    Layout,
    beam_height,
    component_z,
    normalize_layout,
    periscope_heights,
)

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
#: an output of a polarizer or of a polarizing beam splitter not above this share of the arriving
#: power is exactly dark (floating-point residue of crossed polarizers is about 1e-16)
EPS_DARK = 1e-12
#: decimals of ``mw`` and ``source_mw`` (mW) in the output; every other beam number has 9
MW_DECIMALS = 9
#: passes (over or under a component) one whole trace records (contract 3D §3.5)
MAX_TRACE_PASSES = 5000

_DEG = math.pi / 180.0
_INF = math.inf
#: no light at all
_DARK = (0.0, 0.0, 0.0, 0.0)


def _r3(v: float) -> float:
    r = round(v, 3)
    return r + 0.0          # -0.0 -> 0.0


def _r6(v: float) -> float:
    r = round(v, 6)
    return r + 0.0


def _r9(v: float) -> float:
    r = round(v, 9)
    return r + 0.0


def _rmw(v: Optional[float]) -> Optional[float]:
    if v is None:
        return None
    r = round(v, MW_DECIMALS)
    return r + 0.0


def _sig9(v: float) -> float:
    """*v* to 9 significant digits (the front end: ``Number(v.toPrecision(9))``); ``-0.0`` is ``0.0``."""
    return float(f"{v:.9g}") + 0.0


class _Comp:
    """A component prepared for tracing; *h* is the table's beam height."""

    __slots__ = ("index", "id", "type", "role", "cx", "cy", "nx", "ny", "half", "props",
                 "action_kind", "gain", "focal", "retard", "axis_sin", "axis_cos", "z_in", "z_out")

    def __init__(self, index: int, c: Component, h: float) -> None:
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
        # the heights (see Heights): the axis; a periscope's entrance along +x and exit
        if c.type == "periscope":
            self.z_in, self.z_out = periscope_heights(c, h)
        else:
            self.z_in = self.z_out = component_z(c, h)
        # the beam (see Beam in the module docstring)
        p = c.props
        #: the share of the power that goes on after a reflection or a transmission
        self.gain = p.get("throughput", 1.0)
        if c.type == "nd" and p.get("od") is not None:
            self.gain = 10.0 ** (-p["od"])
        #: the focal length that changes q (None: q passes unchanged)
        self.focal = p.get("focal_mm") if c.type in ("lens", "objective") and p.get("focal_mm") else None
        #: a waveplate's (cos delta, sin delta), exact constants; None: no effect on the polarization
        self.retard = {"half": (-1.0, 0.0), "quarter": (0.0, 1.0)}.get(p.get("retardance")) \
            if c.type == "waveplate" else None
        #: the axis of a waveplate or polarizer in the part's own frame, from local +y towards up
        axis = p.get("fast_axis_deg", 0.0) if c.type == "waveplate" else p.get("axis_deg", 0.0)
        self.axis_sin = math.sin(axis * _DEG)
        self.axis_cos = math.cos(axis * _DEG)

    def ports(self, dn: float) -> tuple[float, float]:
        """``(entry, exit)``: the heights where light with ``d . n`` = *dn* enters and leaves (one height for
        everything but a periscope, whose entrance is ``z_in`` along its +x and ``z_out`` the other way)."""
        return (self.z_in, self.z_out) if dn > 0.0 else (self.z_out, self.z_in)

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
        h = beam_height(layout.table)
        self.h = h                  # the table's beam height
        self.comps = [_Comp(i, c, h) for i, c in enumerate(layout.components)]
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

    def nearest_hit(self, px: float, py: float, dx: float, dy: float, last: Optional[_Comp],
                    z: Optional[float] = None) -> Optional[tuple]:
        """``(t, comp, x, y, offset, d.n, dz)`` of the nearest hit of a ray at height *z* (default: the table's
        beam height), or ``None``."""
        return self.meetings(px, py, dx, dy, last, self.h if z is None else z)[0]

    def meetings(self, px: float, py: float, dx: float, dy: float, last: Optional[_Comp],
                 z: float) -> tuple[Optional[tuple], list[tuple]]:
        """``(hit, passed)`` for a ray at height *z*: ``hit`` = ``(t, comp, x, y, offset, d.n, dz)`` of the nearest
        hit, or ``None``; ``passed`` = ``(t, comp, x, y, dz)`` of every meeting in the plane that its height rules
        out (see Heights), in the order of the layout, whatever its ``t``."""
        best: Optional[tuple] = None
        best_t = _INF
        passed: list[tuple] = []
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
            dz = z - c.ports(dn)[0]
            if abs(dz) > c.half + EPS_APERTURE:     # over or under it: the ray goes on
                passed.append((t, c, hx, hy, dz))
                continue
            if t < best_t - EPS_TIE:            # a later component must be nearer by more than the tolerance
                best_t = t
                best = (t, c, hx, hy, offset, dn, dz)
        return best, passed


def _wavelengths(c: _Comp) -> list[Optional[float]]:
    p = c.props
    if p.get("wavelengths_nm"):
        return list(p["wavelengths_nm"])
    if p.get("wavelength_nm") is not None:
        return [p["wavelength_nm"]]
    return [None]


def _interact(c: _Comp, dn: float, offset: float, dz: float, wavelength: Optional[float]) -> tuple[str, Optional[str]]:
    """``(action, end)``: what the hit does, and how the branch ends (``None`` = it goes on;
    ``split`` is handled by the caller). *dz* is the height of the ray minus that of the component's
    axis (only an iris looks at it)."""
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
        if open_mm is not None and (abs(offset) > open_mm / 2.0 + EPS_APERTURE
                                    or abs(dz) > open_mm / 2.0 + EPS_APERTURE):     # in the plane, in height
            return "blocked", "blocked"
        return "transmit", None
    if kind == "shutter":
        return ("blocked", "blocked") if c.props.get("closed") else ("transmit", None)
    return "transmit", None


# ── the beam (see Beam in the module docstring) ─────────────────────────────────────────────


def _scaled(st: tuple, k: float) -> tuple:
    return (k * st[0], k * st[1], k * st[2], k * st[3])


def _mirrored(st: tuple) -> tuple:
    """``R_m S``: a reflection in the plane of the table."""
    return (st[0], st[1], -st[2], -st[3])


def _axis_angle(c: _Comp, dn: float) -> float:
    """The axis of a waveplate / polarizer as the ray sees it (radians, from ``e_h`` towards ``e_v``):
    ``atan2(sin theta * dn, cos theta)``, along ``d x a`` (``a``: the absorbing axis of a polarizer, the
    optic axis of a positive uniaxial plate; in the face, perpendicular to the axis given)."""
    return math.atan2(c.axis_sin * dn, c.axis_cos)


def _polarized(st: tuple, theta: float) -> tuple:
    """``P(theta) S`` of an ideal polarizer, as ``k (1, c, s, 0)``; dark when ``k`` is residue."""
    c = math.cos(2.0 * theta)
    s = math.sin(2.0 * theta)
    k = 0.5 * (st[0] + c * st[1] + s * st[2])
    if k <= EPS_DARK * st[0]:
        return _DARK
    return (k, k * c, k * s, 0.0)


def _retarded(st: tuple, theta: float, cd: float, sd: float) -> tuple:
    """``W(theta, delta) S`` of a retarder, fast axis at *theta*; ``(cd, sd) = (cos, sin) delta``."""
    c = math.cos(2.0 * theta)
    s = math.sin(2.0 * theta)
    s0, s1, s2, s3 = st
    return (s0,
            (c * c + s * s * cd) * s1 + c * s * (1.0 - cd) * s2 + s * sd * s3,
            c * s * (1.0 - cd) * s1 + (s * s + c * c * cd) * s2 - c * sd * s3,
            -s * sd * s1 + c * sd * s2 + cd * s3)


def _pbs_outputs(st: tuple) -> tuple:
    """``(reflected, transmitted)`` of a polarizing beam splitter: ``R_m P(90) S`` and ``P(0) S``."""
    kr = 0.5 * (st[0] - st[1])
    kt = 0.5 * (st[0] + st[1])
    reflected = _DARK if kr <= EPS_DARK * st[0] else (kr, -kr, 0.0, 0.0)
    transmitted = _DARK if kt <= EPS_DARK * st[0] else (kt, kt, 0.0, 0.0)
    return reflected, transmitted


def _through_lens(q: tuple, f: float) -> Optional[tuple]:
    """``1/q' = 1/q - 1/f`` on ``q = (z, zR)`` (``None`` if the result is not a beam, which the
    props the model accepts never give)."""
    z, zr = q
    den = z * z + zr * zr
    a = z / den - 1.0 / f              # Re(1/q')
    b = -zr / den                      # Im(1/q')
    den2 = a * a + b * b
    if not den2 > 0.0:
        return None
    z2 = a / den2
    zr2 = -b / den2
    if not (math.isfinite(z2) and math.isfinite(zr2) and zr2 > 0.0):
        return None
    return (z2, zr2)


def _acted(c: _Comp, action: str, dn: float, st: tuple, q: Optional[tuple]) -> tuple:
    """``(S', q')`` after a ``reflect`` or ``transmit`` at *c* (*dn* = ``d . n`` of the hit)."""
    if action == "reflect":
        return _scaled(_mirrored(st), c.gain), q
    if c.type in ("lens", "objective"):
        return _scaled(st, c.gain), (q if q is None or c.focal is None else _through_lens(q, c.focal))
    if c.type == "waveplate" and c.retard is not None:
        return _scaled(_retarded(st, _axis_angle(c, dn), c.retard[0], c.retard[1]), c.gain), q
    if c.type == "polarizer":
        return _scaled(_polarized(st, _axis_angle(c, dn)), c.gain), q
    return _scaled(st, c.gain), q


def _diameter(q: Optional[tuple], tb: "_TreeBeam") -> Optional[float]:
    if q is None:
        return None
    z, zr = q
    return 2.0 * math.sqrt(tb.lam_mm * tb.m2 * (z * z + zr * zr) / (math.pi * zr))


def _pol_out(st: tuple) -> Optional[list]:
    s0 = st[0]
    if s0 == 0.0:
        return None
    return [_r9(st[1] / s0), _r9(st[2] / s0), _r9(st[3] / s0)]


class _TreeBeam:
    """What one tree's beam has in common: its wavelength in mm, M^2, the source's power."""

    __slots__ = ("lam_mm", "m2", "source_mw", "arrivals")

    def __init__(self, lam_mm: Optional[float], m2: Optional[float], source_mw: Optional[float]) -> None:
        self.lam_mm = lam_mm            # None: unknown wavelength
        self.m2 = m2                    # None: no Gaussian beam in this tree
        self.source_mw = source_mw      # None: the absolute power is unknown
        self.arrivals: dict[str, float] = {}    # branch id -> S0 where it stopped (unrounded)


def _source_beam(src: _Comp, wavelength: Optional[float], source_mw: Optional[float]) -> tuple:
    """``(tree beam, S, q)`` of a ray that leaves *src* (its own props: see Beam)."""
    p = src.props
    st = (1.0, 0.0, 0.0, 0.0)
    if p.get("polarization") == "linear":
        a = 2.0 * p.get("pol_angle_deg", 0.0) * _DEG
        st = (1.0, math.cos(a), math.sin(a), 0.0)
    lam = None if wavelength is None else wavelength * 1e-6
    q = None
    m2 = None
    if lam is not None and lam > 0.0 and p.get("beam_diameter_mm") is not None:
        m2 = p.get("m2", 1.0)
        w0 = p["beam_diameter_mm"] / 2.0
        q = (0.0, math.pi * w0 * w0 / (m2 * lam))
    return _TreeBeam(lam, m2, source_mw), st, q


def _segment(x0: float, y0: float, x1: float, y1: float, z: float, st: tuple, q: Optional[tuple],
             passes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"x0": _r3(x0), "y0": _r3(y0), "x1": _r3(x1), "y1": _r3(y1), "z_mm": _r3(z),
            "beam": {"p": _r9(st[0]), "s": _pol_out(st),
                     "q": None if q is None else [_sig9(q[0]), _sig9(q[1])]},
            "passes": passes}


def _in_order(passed: list[tuple]) -> list[tuple]:
    """*passed* in the order along the ray: by ``t``; meetings whose ``t`` lies within :data:`EPS_TIE` of the
    first of a run of them are at one point (the last bit of a double, which the two languages do not compute
    alike, must not order them) and go by component id (code points)."""
    by_t = sorted(passed, key=lambda p: p[0])
    out: list[tuple] = []
    i = 0
    while i < len(by_t):
        j = i + 1
        while j < len(by_t) and by_t[j][0] - by_t[i][0] <= EPS_TIE:
            j += 1
        out += sorted(by_t[i:j], key=lambda p: p[1].id)
        i = j
    return out


def _passes(passed: list[tuple], end_t: float, inclusive: bool, budget: "_Budget") -> list[dict[str, Any]]:
    """The passes of a segment that ends at *end_t* (see Heights): the meetings its height ruled out with ``t``
    below *end_t* (up to it when *inclusive*), in the order along the ray (:func:`_in_order`), as many as the
    trace may still record."""
    kept = _in_order([p for p in passed if (p[0] <= end_t if inclusive else p[0] < end_t)])
    kept = kept[:max(0, MAX_TRACE_PASSES - budget.passes)]
    budget.passes += len(kept)
    return [{"component": c.id, "x": _r3(hx), "y": _r3(hy), "dz_mm": _r3(dz)} for _t, c, hx, hy, dz in kept]


def _arrival(st: tuple, q: Optional[tuple], tb: _TreeBeam) -> dict[str, Any]:
    d = _diameter(q, tb)
    return {"p": _r9(st[0]), "mw": None if tb.source_mw is None else _rmw(tb.source_mw * st[0]),
            "d_mm": None if d is None else _r9(d), "s": _pol_out(st)}


class _Budget:
    """What the whole trace may still record (see Budget and Heights in the module docstring)."""

    __slots__ = ("limit", "events", "cut", "passes")

    def __init__(self, limit: int) -> None:
        self.limit = limit      # events the trace may record in all
        self.events = 0         # events recorded so far, in every tree
        self.cut = False        # a hit found no room: the trace stops
        self.passes = 0         # passes recorded so far (at most MAX_TRACE_PASSES)


def _split(c: _Comp, fraction: float, st: tuple) -> tuple:
    """``((fraction, S) of the reflected child, (fraction, S) of the transmitted child)``."""
    if c.type == "pbs":
        reflected, transmitted = _pbs_outputs(st)
        if st[0] > 0.0:
            return ((fraction * reflected[0] / st[0], reflected),
                    (fraction * transmitted[0] / st[0], transmitted))
        return (fraction * 0.5, reflected), (fraction * 0.5, transmitted)    # no light: geometry only
    r = c.props.get("reflect_fraction", 0.5)
    return (fraction * r, _scaled(_mirrored(st), r)), (fraction * (1.0 - r), _scaled(st, 1.0 - r))


def _walk(world: _World, budget: _Budget, out: list, bid: str, px: float, py: float, z: float, dx: float,
          dy: float, fraction: float, path: float, n_events: int, last: Optional[_Comp],
          wavelength: Optional[float], tb: _TreeBeam, st: tuple, q: Optional[tuple]) -> None:
    """Follow one branch from ``(px, py)`` at height *z* along ``(dx, dy)``; *st* and *q* are the beam there."""
    branch: dict[str, Any] = {"branch": bid, "fraction": _r6(fraction), "segments": [],
                              "events": [], "end": ""}
    out.append(branch)
    while True:
        limit = min(world.distance_to_edge(px, py, dx, dy), MAX_SEGMENT_MM)
        hit, passed = world.meetings(px, py, dx, dy, last, z)
        if hit is None or hit[0] > limit:
            branch["segments"].append(_segment(px, py, px + limit * dx, py + limit * dy, z, st, q,
                                               _passes(passed, limit, True, budget)))
            branch["end"] = "left_table"
            return
        t, c, hx, hy, offset, dn, dz = hit
        path += t
        branch["segments"].append(_segment(px, py, hx, hy, z, st, q, _passes(passed, t - EPS_TIE, False, budget)))
        if budget.events >= budget.limit:       # no room for this hit: not recorded, the trace stops
            branch["end"] = "budget"
            budget.cut = True
            return
        budget.events += 1
        if q is not None:
            q = (q[0] + t, q[1])                # the beam has travelled t to the hit
        action, end = _interact(c, dn, offset, dz, wavelength)
        branch["events"].append({
            "component": c.id, "action": action, "x": _r3(hx), "y": _r3(hy), "z_mm": _r3(z),
            "offset_mm": _r3(offset), "dz_mm": _r3(dz),
            "incidence_deg": _r3(math.acos(min(1.0, abs(dn))) * (180.0 / math.pi)),
            "path_mm": _r3(path),
            "beam": _arrival(st, q, tb)})
        n_events += 1
        last = c
        px, py = hx, hy
        reflected = (dx - 2.0 * dn * c.nx, dy - 2.0 * dn * c.ny)
        if action == "split":
            if n_events >= MAX_EVENTS:
                branch["end"] = "max_events"
                return
            branch["end"] = "split"
            (fr, st_r), (ft, st_t) = _split(c, fraction, st)
            for suffix, f, direction, st_child in ((".r", fr, reflected, st_r), (".t", ft, (dx, dy), st_t)):
                if f < MIN_FRACTION:
                    continue
                _walk(world, budget, out, bid + suffix, px, py, z, direction[0], direction[1], f, path,
                      n_events, last, wavelength, tb, st_child, q)
                if budget.cut:
                    return
            return
        if end is not None:
            if end == "stop":
                tb.arrivals[bid] = st[0]        # what a fibre coupler here passes on
            branch["end"] = end
            return
        st, q = _acted(c, action, dn, st, q)
        if action == "reflect":
            dx, dy = reflected
        entry, exit_ = c.ports(dn)
        if exit_ != entry:                      # a periscope that goes up or down: the vertical run (see Heights)
            z = z + (exit_ - entry)
            rise = abs(exit_ - entry)
            path += rise
            if q is not None:
                q = (q[0] + rise, q[1])
        if n_events >= MAX_EVENTS:
            branch["end"] = "max_events"
            return


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
            queue.append((c, wl, c.props.get("power_mw")))
            seen.add((c.id, wl))
    trees: list[dict[str, Any]] = []
    while queue:
        src, wl, source_mw = queue.popleft()
        tb, st, q = _source_beam(src, wl, source_mw)
        tree: dict[str, Any] = {"source": src.id, "wavelength_nm": wl,
                                "beam": {"source_mw": _rmw(source_mw),
                                         "m2": None if q is None else _r9(tb.m2)},
                                "branches": []}
        _walk(world, budget, tree["branches"], "0", src.cx, src.cy, src.z_in, src.nx, src.ny, 1.0, 0.0, 0, None,
              wl, tb, st, q)
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
            fed = None              # the first branch to arrive feeds the fibre (one tree per output and wavelength)
            if tb.source_mw is not None:
                fed = tb.source_mw * tb.arrivals[br["branch"]] * last.props.get("coupling", 1.0)
            queue.append((nxt, wl, fed))
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
    centre (``|offset| <= aperture``), and the segment is at the component's height (``|z - z_port|
    <= aperture / 2 + 1e-9``, contract 3D §3.6: at another height a crossing beside it means nothing;
    a segment without ``z_mm`` is at the table's beam height). Result items: ``{"component", "source",
    "wavelength_nm", "branch", "x", "y", "offset_mm", "outside_mm"}`` (``outside_mm`` = how far
    beyond the edge), in trace order. Components that other rays do hit are not excluded here;
    the summary decides what is worth saying.
    """
    lay = normalize_layout(layout)
    h = beam_height(lay.table)
    comps = [_Comp(i, c, h) for i, c in enumerate(lay.components)]
    targets = [c for c in comps if c.action_kind != "source"]
    out: list[dict[str, Any]] = []
    for tree in trace:
        for br in tree["branches"]:
            for seg in br["segments"]:
                sx, sy = seg["x0"], seg["y0"]
                ex, ey = seg["x1"], seg["y1"]
                z = seg.get("z_mm", h)
                length = math.hypot(ex - sx, ey - sy)
                if length <= EPS_SELF:
                    continue
                dx, dy = (ex - sx) / length, (ey - sy) / length
                for c in targets:
                    dn = dx * c.nx + dy * c.ny
                    if abs(dn) < EPS_PARALLEL:
                        continue
                    if abs(z - c.ports(dn)[0]) > c.half + EPS_APERTURE:
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
    "EPS_APERTURE", "EPS_DARK", "EPS_PARALLEL", "EPS_SELF", "EPS_T", "EPS_TIE", "MAX_EVENTS",
    "MAX_SEGMENT_MM", "MAX_TRACE_EVENTS", "MAX_TRACE_PASSES", "MIN_FRACTION", "MW_DECIMALS",
    "NEAR_MISS_APERTURES", "TABLE_MARGIN_MM", "lit_components", "near_misses", "trace_layout", "truncation",
]
