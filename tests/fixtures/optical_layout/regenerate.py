"""Regenerate the expected traces of the shared optical-layout fixtures (the ``*.json`` next to this file).

    $env:PYTHONPATH = "MASTv2"
    .venv-v2-py313\\Scripts\\python.exe tests/fixtures/optical_layout/regenerate.py          # rewrite
    .venv-v2-py313\\Scripts\\python.exe tests/fixtures/optical_layout/regenerate.py --check  # compare only

Every fixture gets its ``expected_trace`` (and its ``expected_misses``, where it has that key) from the
Python tracer again; ``name``, ``description``, ``layout`` and ``trace_options`` stay as they are. The
fixtures of the beam (contract ``docs/v2/design/optical_layout_v2.md`` §7) are defined in :data:`BEAM`
below, those of the heights (``docs/v2/design/optical_layout_3d.md`` §9) in :data:`HEIGHTS`: their
layouts are built here, so a change to one of them is made here and the file written again.
``--check`` writes nothing and exits 1 when a file would change.

A fixture is a pact between two implementations (Python here, TypeScript in the front end), so the
generator refuses (exit 2) to write a number the two could round apart or a decision they could take
apart:

* no number rounded to 0.001 (coordinates, lengths, angles) or to 1e-6 (``fraction``, and ``mw`` /
  ``source_mw`` when :data:`~mast.optical_layout.trace.MW_DECIMALS` is 6) lies within a millionth of its
  last place of a rounding tie - Python's ``round`` and JavaScript's ``Math.round`` part ways on ties,
  and the comparison's tolerance (1e-6) is not wider than that last place;
* no branch of a polarizing beam splitter is within a millionth (relative) of the 1e-3 floor below which
  a branch is not created (its fraction comes from trigonometry);
* no output of a polarizer or a polarizing beam splitter is within a factor of ten of the dark threshold
  (:data:`~mast.optical_layout.trace.EPS_DARK` times the arriving power);
* no two meetings a segment passes over or under, and no such meeting and the hit that ends the segment,
  are between 1e-10 and 1e-6 mm apart along the ray (``t``), and none is within 1e-6 mm of the end of a
  segment that leaves the table: either they are at one point (and ordered by the tie rule) or clearly
  apart, never where the last bits of a double could decide.

After a change: check the physics by hand (``tests/v2/unit/optical_layout/test_layout_beam_physics.py``
re-derives the anchors of the contract independently), tell the front end, and never regenerate only to
make a test green. Line endings of a file are kept as they are (a new file takes those of README.md).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "MASTv2"))

from mast.optical_layout import trace as T  # noqa: E402
from mast.optical_layout.model import Layout  # noqa: E402

#: JSON files of this folder that are not trace fixtures (how powers are written, contract v2 §12.3)
NOT_TRACES = frozenset({"power_text_cases.json"})
#: what the server would have written into a stored layout (as in every fixture)
STAMP = {"revision": 1, "updated_at": "2026-01-01T00:00:00+00:00", "updated_by": "fixture"}


def comp(cid: str, typ: str, x: float, y: float, angle: float = 0.0, *, label: str | None = None,
         aperture: float | None = None, **props) -> dict:
    c = {"id": cid, "type": typ, "label": cid.upper() if label is None else label, "x_mm": x, "y_mm": y,
         "angle_deg": angle, "props": props}
    if aperture is not None:
        c["aperture_mm"] = aperture
    return c


def layout(lid: str, title: str, components: list[dict], *, width: float = 1200.0, height: float = 900.0,
           beam_height: float | None = None) -> dict:
    """The canonical form of a layout (as the server stores it)."""
    table: dict = {"width_mm": width, "height_mm": height}
    if beam_height is not None:
        table["beam_height_mm"] = beam_height
    raw = {"schema": 1, "id": lid, "title": title, **STAMP, "table": table, "components": components}
    return Layout.model_validate(raw).to_dict()


# ── the fixtures of the beam (contract v2 §7) ────────────────────────────────────────────────


def _malus() -> dict:
    return layout("malus", "Malus: two polarizers", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=633, power_mw=10, polarization="linear",
             pol_angle_deg=0),
        comp("pol1", "polarizer", 150, 100, 0, label="P1", axis_deg=30),
        comp("pol2", "polarizer", 250, 100, 0, label="P2", axis_deg=90),
        comp("det1", "detector", 350, 100, 180, label="D1"),
    ])


def _hwp() -> dict:
    return layout("hwp", "Half-wave plate turns the polarization, PBS splits", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=532, power_mw=20, polarization="linear"),
        comp("hwp1", "waveplate", 150, 100, 0, label="HWP1", retardance="half", fast_axis_deg=22.5),
        comp("pbs1", "pbs", 250, 100, 135, label="PBS1"),
        comp("det_r1", "detector", 250, 300, 270, label="DR1"),
        comp("det_t1", "detector", 450, 100, 180, label="DT1"),
        comp("las2", "laser", 50, 500, 0, label="L2", wavelength_nm=532, power_mw=20, polarization="linear"),
        comp("hwp2", "waveplate", 150, 500, 0, label="HWP2", retardance="half", fast_axis_deg=15),
        comp("pbs2", "pbs", 250, 500, 135, label="PBS2"),
        comp("det_r2", "detector", 250, 700, 270, label="DR2"),
        comp("det_t2", "detector", 450, 500, 180, label="DT2"),
    ])


def _isolator() -> dict:
    return layout("isolator", "Quarter-wave isolator", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=780, power_mw=5, polarization="linear",
             pol_angle_deg=0),
        comp("pbs1", "pbs", 150, 100, 135, label="PBS1"),
        comp("qwp1", "waveplate", 250, 100, 0, label="QWP1", retardance="quarter", fast_axis_deg=45),
        comp("m1", "mirror", 350, 100, 180, label="M1"),
        comp("iso", "beam_dump", 150, 20, 90, label="DUMP"),
    ])


def _mirror_flip() -> dict:
    return layout("flip", "A mirror turns +45 into -45", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=532, power_mw=1, polarization="linear",
             pol_angle_deg=45),
        comp("m1", "mirror", 150, 100, 135, label="M1"),
        comp("bs1", "beam_splitter", 150, 300, 315, label="BS1", reflect_fraction=0.3),
        comp("det_r", "detector", 350, 300, 180, label="DR"),
        comp("det_t", "detector", 150, 500, 270, label="DT"),
    ])


def _pbs_split() -> dict:
    return layout("pbs-split", "PBS: +45 and unpolarized light; a polarizer on a lamp", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=633, power_mw=10, polarization="linear",
             pol_angle_deg=45),
        comp("pbs1", "pbs", 250, 100, 135, label="PBS1"),
        comp("det_r1", "detector", 250, 300, 270, label="DR1"),
        comp("det_t1", "detector", 450, 100, 180, label="DT1"),
        comp("las2", "laser", 50, 500, 0, label="L2", wavelength_nm=633, power_mw=10),
        comp("pbs2", "pbs", 250, 500, 135, label="PBS2"),
        comp("det_r2", "detector", 250, 700, 270, label="DR2"),
        comp("det_t2", "detector", 450, 500, 180, label="DT2"),
        comp("lamp1", "lamp", 600, 820, 0, label="LAMP", wavelength_nm=550),
        comp("pol3", "polarizer", 800, 820, 0, label="P3", axis_deg=60),
        comp("det3", "detector", 1000, 820, 180, label="D3"),
    ])


def _pbs_p_only() -> dict:
    return layout("pbs-p", "p into a PBS only transmits, s only reflects", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=532, polarization="linear", pol_angle_deg=0),
        comp("pbs1", "pbs", 250, 100, 135, label="PBS1"),
        comp("det_r1", "detector", 250, 300, 270, label="DR1"),
        comp("det_t1", "detector", 450, 100, 180, label="DT1"),
        comp("las2", "laser", 50, 500, 0, label="L2", wavelength_nm=532, polarization="linear", pol_angle_deg=90),
        comp("pbs2", "pbs", 250, 500, 135, label="PBS2"),
        comp("det_r2", "detector", 250, 700, 270, label="DR2"),
        comp("det_t2", "detector", 450, 500, 180, label="DT2"),
    ])


def _oblique() -> dict:
    return layout("oblique", "Waveplate and polarizer axes seen obliquely and from behind", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=633, polarization="linear"),
        comp("hwp1", "waveplate", 250, 100, 40, label="HWP1", retardance="half", fast_axis_deg=30),
        comp("det1", "detector", 450, 100, 180, label="D1"),
        comp("las2", "laser", 1150, 300, 180, label="L2", wavelength_nm=633, polarization="linear"),
        comp("hwp2", "waveplate", 950, 300, 0, label="HWP2", retardance="half", fast_axis_deg=30),
        comp("det2", "detector", 750, 300, 0, label="D2"),
        comp("las3", "laser", 50, 500, 0, label="L3", wavelength_nm=633, polarization="linear"),
        comp("hwp3", "waveplate", 250, 500, 0, label="HWP3", retardance="half", fast_axis_deg=30),
        comp("det3", "detector", 450, 500, 180, label="D3"),
        comp("las4", "laser", 50, 700, 0, label="L4", wavelength_nm=633),
        comp("pol4", "polarizer", 250, 700, 40, label="P4", axis_deg=30),
        comp("det4", "detector", 450, 700, 180, label="D4"),
    ])


def _power_fibre() -> dict:
    return layout("power-fibre", "100 mW through OD 1, a 90 % mirror and a 60 % fibre coupling", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=532, power_mw=100),
        comp("nd1", "nd", 150, 100, 0, label="ND1", od=1),
        comp("m1", "mirror", 250, 100, 135, label="M1", throughput=0.9),
        comp("fi1", "fiber_in", 250, 300, 270, label="FC1", continues_at="fo1", coupling=0.6),
        comp("fo1", "fiber_out", 600, 100, 0, label="FO1", wavelength_nm=532, polarization="linear",
             pol_angle_deg=90, beam_diameter_mm=2, m2=1.1),
        comp("pm1", "power_meter", 900, 100, 180, label="PM1"),
    ])


def _fibre_first_arrival() -> dict:
    return layout("fibre-first", "Two branches reach one fibre: the first one feeds it", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=633, power_mw=50),
        comp("bs1", "beam_splitter", 250, 100, 135, label="BS1", reflect_fraction=0.3),
        comp("m_r", "mirror", 250, 400, 315, label="MR"),
        comp("m_t", "mirror", 600, 100, 135, label="MT"),
        comp("fi1", "fiber_in", 600, 400, 225, label="FC1", continues_at="fo1", coupling=0.5),
        comp("fo1", "fiber_out", 600, 700, 0, label="FO1", wavelength_nm=633),
        comp("pm1", "power_meter", 900, 700, 180, label="PM1"),
    ])


def _throughput_chain() -> dict:
    return layout("throughput", "The throughput of every kind of part", [
        comp("las1", "laser", 20, 100, 0, label="L1", wavelength_nm=532, power_mw=10),
        comp("win", "window", 100, 100, 0, label="WIN", throughput=0.95),
        comp("lens", "lens", 150, 100, 0, label="LENS", throughput=0.98),
        comp("filt", "filter", 200, 100, 0, label="FILT", pass_min_nm=500, throughput=0.9),
        comp("aotf", "aotf", 250, 100, 0, label="AOTF", throughput=0.5),
        comp("prism", "prism", 300, 100, 0, label="PRISM", throughput=0.99),
        comp("peri", "periscope", 350, 100, 0, label="PERI", throughput=0.97),
        comp("cust_t", "custom", 400, 100, 0, label="CT", behavior="transmit", throughput=0.8),
        comp("wp", "waveplate", 450, 100, 0, label="WP", throughput=0.9),
        comp("pol", "polarizer", 500, 100, 0, label="POL", throughput=0.8),
        comp("obj", "objective", 550, 100, 0, label="OBJ", throughput=0.7),
        comp("iris", "iris", 600, 100, 0, label="IRIS", open_mm=5),
        comp("shut", "shutter", 625, 100, 0, label="SHUT", closed=False),
        comp("grat", "grating", 650, 100, 135, label="GRAT", throughput=0.7),
        comp("smp", "sample", 650, 300, 315, label="SMP", reflects=True, throughput=0.6),
        comp("cust_r", "custom", 850, 300, 135, label="CR", behavior="reflect", throughput=0.5),
        comp("det", "detector", 850, 500, 270, label="D"),
    ])


def _gauss_free() -> dict:
    return layout("gauss-free", "A Gaussian beam spreading in free space", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=633, beam_diameter_mm=1),
        comp("win1", "window", 550, 100, 0, label="W1"),
        comp("det1", "detector", 1050, 100, 180, label="D1"),
        comp("las2", "laser", 50, 500, 0, label="L2", wavelength_nm=633, beam_diameter_mm=1, m2=2),
        comp("det2", "detector", 1050, 500, 180, label="D2"),
    ])


def _kepler() -> dict:
    return layout("kepler", "Keplerian beam expander 3x (f 50 / 150)", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=532, beam_diameter_mm=1),
        comp("lens1", "lens", 150, 100, 0, label="F50", focal_mm=50),
        comp("lens2", "lens", 350, 100, 0, label="F150", focal_mm=150),
        comp("det1", "detector", 1350, 100, 180, label="D1", aperture=25.4),
    ], width=1500.0)


def _objective() -> dict:
    return layout("objective", "A collimated 4 mm beam focused by an f = 4 mm objective onto a sample", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=633, beam_diameter_mm=4, aperture=5),
        comp("obj1", "objective", 100, 100, 0, label="OBJ", focal_mm=4),
        comp("smp1", "sample", 104, 100, 180, label="SMP"),
    ])


def _multi_wavelength() -> dict:
    return layout("two-colours", "One source, two wavelengths, a dichroic: each its own Rayleigh range", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelengths_nm=[488, 785], power_mw=30, polarization="linear",
             pol_angle_deg=45, beam_diameter_mm=1.5, m2=1.2),
        comp("dm1", "dichroic", 250, 100, 135, label="DM1", edge_nm=600, reflects="short"),
        comp("det_b", "detector", 250, 600, 270, label="DB"),
        comp("det_r", "detector", 1050, 100, 180, label="DR"),
    ])


def _crossed() -> dict:
    return layout("crossed", "Crossed polarizers: no light, but the beam goes on", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=532, power_mw=10, polarization="linear"),
        comp("pol1", "polarizer", 150, 100, 0, label="P1", axis_deg=30),
        comp("pol2", "polarizer", 250, 100, 0, label="P2", axis_deg=120),
        comp("m1", "mirror", 350, 100, 135, label="M1"),
        comp("pbs1", "pbs", 350, 300, 315, label="PBS1"),
        comp("det_r", "detector", 550, 300, 180, label="DR"),
        comp("det_t", "detector", 350, 500, 270, label="DT"),
    ])


#: name -> (description, layout builder)
BEAM: dict[str, tuple[str, object]] = {
    "pol_malus_two_polarizers": (
        "Anchor A (Malus). A 10 mW 633 nm laser, linear 0 deg (in the table plane, p), meets polarizer P1 "
        "(axis 30 deg) and then P2 (axis 90 deg), both at normal incidence from the front. After P1: "
        "p = cos^2(30) = 0.75, 7.5 mW, linear 30 deg (s = [0.5, 0.866025, 0]); after P2: p = 0.75 cos^2(60) = "
        "0.1875, 1.875 mW, vertical (s = [-1, 0, 0]).", _malus),
    "pol_hwp_rotates": (
        "Anchor B. Horizontal light through a half-wave plate turns to 2 theta: fast axis 22.5 deg gives +45 "
        "(arriving at PBS1 with s = [0, 1, 0], p = 1), which the PBS splits 50 / 50 - transmitted horizontal "
        "(p), reflected vertical (s): fractions 0.5 / 0.5, the PBS ratio is in the geometric fraction. Fast "
        "axis 15 deg gives 30 deg: transmitted cos^2(30) = 0.75, reflected 0.25 - the adjustable attenuator.",
        _hwp),
    "pol_qwp_isolator": (
        "Anchor C2. Horizontal light passes PBS1 (all transmitted: the reflected branch is below the floor "
        "and not created), a quarter-wave plate at 45 deg makes it circular (s = [0, 0, -1], chi = -45: "
        "anchor C), the mirror at normal incidence turns the handedness (R_m: [0, 0, 1]), the same plate "
        "seen from behind (axis 180 - 45 = 135 deg) makes it vertical (s = [-1, 0, 0]), and PBS1 sends all "
        "of it out of its reflecting port into the dump (branch 0.t.r, fraction 1): an optical isolator.",
        _isolator),
    "pol_mirror_flips_diagonal": (
        "Anchor D. +45 deg light (s = [0, 1, 0]) on a fold mirror leaves as -45 deg (s = [0, -1, 0]): a "
        "reflection keeps the in-plane field and turns the vertical one (Mueller diag(1, 1, -1, -1)) - the "
        "angle follows the beam's own frame, e_h = z x d. The beam splitter BS1 (R = 0.3) does the same to "
        "its reflected branch (p = 0.3, back to +45) and passes the transmitted one unchanged (p = 0.7, -45).",
        _mirror_flip),
    "pol_pbs_unpolarized_split": (
        "Anchors E and E3. +45 deg light into PBS1: transmitted p = 0.5 horizontal, reflected p = 0.5 "
        "vertical (fractions 0.5 / 0.5). Unpolarized light (no polarization given) into PBS2: the same "
        "halves, each fully polarized. Unpolarized light of a lamp through polarizer P3 (axis 60 deg): "
        "p = 0.5, linear 60 deg (s = [-0.5, 0.866025, 0]); a lamp has no absolute power (mw null).",
        _pbs_split),
    "pol_pbs_p_only": (
        "p (0 deg) into a PBS is transmitted entirely: the reflected branch carries no light, its fraction "
        "0 is below the floor and it is not created (branches 0, 0.t; DR1 is never reached). s (90 deg) is "
        "reflected entirely (branches 0, 0.r; DT2 never reached). No power_mw: mw is null throughout.",
        _pbs_p_only),
    "pol_waveplate_oblique": (
        "The axis of a part as the ray sees it, theta_eff = atan2(sin theta (d . n), cos theta) (contract v2 "
        "§4.5: an absorbing polarizer passes d x a, a positive uniaxial plate's fast wave is along d x c), anchor "
        "'projection'. Half-wave plates with the fast axis at 30 deg in their own frame on horizontal light: "
        "HWP1 hit at 40 deg incidence sees 23.858655 deg and gives linear 47.717310 deg; HWP2 hit from behind "
        "sees -30 (= 150) deg and gives 120 deg (s = [-0.5, -0.866025, 0]); HWP3 from the front at normal "
        "incidence sees 30 and gives 60 deg (s = [-0.5, 0.866025, 0]). Unpolarized light through polarizer P4 "
        "(axis 30 deg, 40 deg incidence) leaves linear along the axis it sees, 23.858655 deg, p = 0.5.",
        _oblique),
    "power_nd_throughput_fibre": (
        "Absolute power. 100 mW, ND OD 1 (x 10^-1): 10 mW at M1; M1 reflects 90 % (throughput 0.9): 9 mW "
        "at the fibre coupler FC1. The fibre output FO1 continues it with source_mw = 9 x coupling 0.6 = "
        "5.4 mW and its own beam: linear 90 deg (s = [-1, 0, 0]) and a 2 mm waist with M^2 = 1.1 (zR = "
        "pi 1^2 / (1.1 x 532e-6) mm); its relative power starts again at p = 1.", _power_fibre),
    "power_fibre_first_arrival": (
        "Two branches of one beam (BS1, R = 0.3) reach the same fibre coupler FC1 (coupling 0.5), the "
        "reflected one first in branch order. One tree per (fibre output, wavelength): FO1 is fed by that "
        "first branch only - source_mw = 50 x 0.3 x 0.5 = 7.5 mW, not the sum of both branches (25 mW).",
        _fibre_first_arrival),
    "power_throughput_chain": (
        "The throughput of every part that takes one (window, lens without a focal length, filter, AOTF, "
        "prism, periscope, custom transmit, waveplate without retardance - transparent to the polarization "
        "-, polarizer, objective without a focal length, grating, reflecting sample, custom reflect), and "
        "an open iris and an open shutter that take none: 10 mW of unpolarized light, the product of all, "
        "the polarizer halving it and making it linear 0 deg; the reflections then keep it horizontal.",
        _throughput_chain),
    "gauss_free_space": (
        "Anchor F. 633 nm, 1/e^2 diameter 1 mm at the exit (the waist), M^2 = 1: zR = pi 0.5^2 / 633e-6 = "
        "1240.755392 mm; 500 mm on (window W1) d = sqrt(1 + (500 / zR)^2) mm; 1000 mm on (D1) d = 1.284357 mm. "
        "L2 has M^2 = 2: zR is halved and d grows faster. The window does not change q.", _gauss_free),
    "gauss_kepler_3x": (
        "Anchor G. 532 nm, 1 mm waist at the laser; lens f = 50 mm 100 mm on, lens f = 150 mm 200 mm after it "
        "(a Keplerian 3x expander, focus between the lenses): d = 1.002291 mm at the first lens, 3.000765 mm "
        "at the second, 3.014325 mm 1000 mm after it (at D1). 1/q' = 1/q - 1/f at each lens.", _kepler),
    "gauss_objective_focus": (
        "Anchor H. 633 nm, 4 mm collimated (waist at the laser), an objective f = 4 mm 50 mm on: after it "
        "q = [-4.000002, zR'] - the waist 4.000002 mm behind the objective, 2 sqrt(lambda zR' / pi) = "
        "0.805958 um across (the estimate lambda f / (pi w) gives 0.805961 um). The sample sits 4.00 mm "
        "behind the objective, in its back focal plane, 1.9 nm before the waist: d there = 0.805961 um "
        "(= lambda f / (pi w0) exactly, the size in the back focal plane whatever the input waist).",
        _objective),
    "gauss_multi_wavelength": (
        "One source with two wavelengths (488 and 785 nm), each tree with all of power_mw (30 mW each), the "
        "same 1.5 mm waist and M^2 = 1.2, so each its own zR = pi 0.75^2 / (1.2 lambda). The dichroic "
        "(edge 600 nm, reflects short) reflects 488 (+45 deg turns to -45 deg: s = [0, -1, 0]) and "
        "transmits 785 (stays +45 deg, s = [0, 1, 0]).", _multi_wavelength),
    "beam_crossed_polarizers_no_light": (
        "Polarizers at 30 and 120 deg are crossed: after P2 there is no light. In floating point the power "
        "after P2 is a residue of about 1e-17, which the dark rule (below EPS_DARK x the arriving power) "
        "makes exactly 0: p = 0, s = null, mw = 0. The geometry goes on as before - the mirror, then a PBS "
        "that receives no light splits 0.5 / 0.5 (fractions), both branches with p = 0.", _crossed),
}


# ── the fixtures of the heights (contract 3D §9) ─────────────────────────────────────────────


def _periscope_up() -> dict:
    return layout("periscope-up", "A periscope lifts the beam from 75 to 250 mm", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=532, power_mw=10, beam_diameter_mm=1),
        comp("per1", "periscope", 200, 100, 0, label="P1", z_out_mm=250),
        comp("m_low", "mirror", 400, 100, 135, label="M_LOW"),
        comp("m_high", "mirror", 600, 100, 135, label="M_HIGH", z_mm=250),
        comp("det1", "detector", 600, 400, 270, label="D1", z_mm=250),
    ])


def _periscope_down() -> dict:
    return layout("periscope-down", "Down through a periscope and back up through it, 3 mm above the axes", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=633, beam_diameter_mm=2, z_mm=203),
        comp("per1", "periscope", 200, 100, 0, label="P1", z_mm=200, z_out_mm=60),
        comp("m1", "mirror", 400, 100, 180, label="M1", z_mm=60),
        comp("det1", "detector", 20, 100, 0, label="D1", z_mm=200),
    ])


def _passes_over_and_under() -> dict:
    return layout("passes", "The beam at 75 mm passes under, over, hits at the edge, passes by 0.01 mm", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=532),
        comp("m_hi", "mirror", 200, 100, 135, label="M_HI", z_mm=100),
        comp("m_lo", "mirror", 300, 100, 135, label="M_LO", z_mm=50),
        comp("m_edge", "mirror", 400, 100, 135, label="M_EDGE", z_mm=87.7),
        comp("m_out", "mirror", 400, 300, 315, label="M_OUT", z_mm=87.71),
        comp("iris1", "iris", 400, 400, 90, label="IRIS", z_mm=77, open_mm=5),
        comp("det1", "detector", 400, 500, 270, label="D1"),
    ])


def _source_heights() -> dict:
    return layout("sources", "Sources at their own heights; a table beam height of 120 mm", [
        comp("las_a", "laser", 50, 100, 0, label="LA", wavelength_nm=532),
        comp("det_a", "detector", 300, 100, 180, label="DA"),
        comp("las_b", "laser", 50, 200, 0, label="LB", wavelength_nm=633, z_mm=140),
        comp("m1", "mirror", 200, 200, 135, label="M1"),
        comp("det_b", "detector", 400, 200, 180, label="DB", z_mm=140),
        comp("las_c", "laser", 50, 300, 0, label="LC", wavelength_nm=785),
        comp("fi1", "fiber_in", 200, 300, 180, label="FC1", continues_at="fo1"),
        comp("fo1", "fiber_out", 600, 400, 180, label="FO1", wavelength_nm=785, z_mm=90),
        comp("m2", "mirror", 450, 400, 315, label="M2"),
        comp("det_c", "detector", 300, 400, 0, label="DC", z_mm=90),
        comp("lamp1", "lamp", 50, 700, 0, label="LAMP", wavelength_nm=550, z_mm=60),
        comp("det_l", "detector", 300, 700, 180, label="DL", z_mm=60),
    ], beam_height=120)


def _iris_in_height() -> dict:
    return layout("iris-height", "An iris in height: passed over, blocked, through its opening", [
        # three beams at 75 mm meet three irises (aperture 25.4, opening 5) whose axes are at different heights
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=532),
        comp("iris_over", "iris", 200, 100, 0, label="I_OVER", z_mm=40, open_mm=5),       # dz +35 > 12.7: passed
        comp("det1", "detector", 400, 100, 180, label="D1"),
        comp("las2", "laser", 50, 300, 0, label="L2", wavelength_nm=532),
        comp("iris_block", "iris", 200, 300, 0, label="I_BLOCK", z_mm=78, open_mm=5),     # dz -3, > 2.5: blocked
        comp("det2", "detector", 400, 300, 180, label="D2"),
        comp("las3", "laser", 50, 500, 0, label="L3", wavelength_nm=532),
        comp("iris_open", "iris", 200, 500, 0, label="I_OPEN", z_mm=77.5, open_mm=5),     # dz -2.5 = 2.5: through
        comp("det3", "detector", 400, 500, 180, label="D3"),
    ])


def _mixed() -> dict:
    return layout("mixed", "Heights with a split, a Gaussian beam, stacked mirrors, an iris and a periscope", [
        comp("las1", "laser", 50, 100, 0, label="L1", wavelength_nm=785, power_mw=20, beam_diameter_mm=1.5, z_mm=78),
        comp("bs1", "beam_splitter", 150, 100, 135, label="BS1", reflect_fraction=0.5),
        # the transmitted branch: two mirrors stacked at one place; the one at the beam's height is hit
        comp("m_b", "mirror", 300, 100, 135, label="M_B", z_mm=300),
        comp("m_a", "mirror", 300, 100, 135, label="M_A"),
        comp("iris1", "iris", 300, 250, 90, label="IRIS", open_mm=4),
        # the reflected branch: under a high mirror, up a periscope, on at 303 mm
        comp("m_p", "mirror", 150, 180, 315, label="M_P", z_mm=200),
        comp("per1", "periscope", 150, 250, 90, label="P1", z_out_mm=300),
        comp("m_up", "mirror", 150, 500, 315, label="M_UP", z_mm=300),
        comp("m_nm", "mirror", 400, 514, 135, label="M_NM"),
        comp("m_nm2", "mirror", 500, 514, 135, label="M_NM2", z_mm=300),
        comp("det_small", "detector", 800, 500, 180, label="D_S", aperture=6, z_mm=300),
    ])


#: name -> (description, layout builder, with expected_misses)
HEIGHTS: dict[str, tuple[str, object, bool]] = {
    "height_periscope_up": (
        "A periscope (contract 3D §3.4) lifts a beam at the table's beam height (75 mm, not given) to its exit "
        "height z_out_mm 250, light going along its +x (d . n > 0: entrance z_mm, exit z_out_mm). The segment after it "
        "is at 250 mm and passes over M_LOW (axis at 75 mm, dz +175 > 12.7: no effect, a pass); M_HIGH and D1 at 250 mm "
        "are hit. The vertical run (175 mm) is path: path_mm 150 at P1, 150 + 175 + 400 = 725 at M_HIGH, 1025 at D1, and "
        "the Gaussian beam (532 nm, 1 mm, zR = pi 0.5^2 / 532e-6 = 1476.312 mm) spreads over it: d = 1.114077 mm at "
        "M_HIGH (1.067143 without the run).", _periscope_up, False),
    "height_periscope_down": (
        "A periscope that goes down (entrance z_mm 200, exit z_out_mm 60) met both ways, the beam 3 mm above the axes. "
        "L1 at 203 mm enters P1 along its +x 3 mm above the entrance (dz +3) and leaves 3 mm above the exit, at 63 mm "
        "(two parallel 45 degree mirrors move the beam as a whole); M1 at 60 mm reflects it straight back (dz +3); it "
        "meets P1 against its +x (d . n < 0), so it enters at z_out_mm 60 (dz +3) and leaves at z_mm + 3 = 203 mm, "
        "towards D1 at 200 mm (dz +3). Heights 203, 63, 63, 203; path_mm 150, 490, 690, 1010 (each pass of P1 adds "
        "its 140 mm run); 633 nm, 2 mm: zR = 4963.022 mm, d = 2.040994 mm at D1.", _periscope_down, False),
    "height_passes_over_and_under": (
        "A beam at 75 mm and mirrors at other heights (aperture 25.4, half 12.7): it passes under M_HI (axis 100, dz "
        "-25) and over M_LO (axis 50, dz +25), in the order along it; it hits M_EDGE (axis 87.7, dz -12.7: exactly at "
        "the edge, which belongs to the aperture as in the plane) and turns to +y; it passes under M_OUT (axis 87.71, "
        "dz -12.71: 0.01 mm beyond the edge); the iris IRIS (axis 77, opening 5) lets it through (|dz| 2 <= 2.5); D1 "
        "at 75 mm stops it. A pass has no effect and is no event.", _passes_over_and_under, False),
    "height_source_heights": (
        "Sources emit at their own heights; the table's beam height is 120 mm. LA (no z_mm, so 120) reaches DA "
        "(120). LB at 140 passes over M1 (120, dz +20) and reaches DB at 140. LC (120) reaches the fibre coupler FC1 "
        "(120), and its continuation FO1 emits at its own height, 90 mm: it passes under M2 (120, dz -30) and "
        "reaches DC at 90. The lamp at 60 reaches DL at 60.", _source_heights, False),
    "height_iris_in_height": (
        "The three cases of an iris in height (contract 3D §3.3), each a beam at 75 mm: I_OVER (axis 40, dz +35 > "
        "12.7, half its aperture) is passed over and the beam reaches D1; I_BLOCK (axis 78, dz -3, within its "
        "aperture but beyond half its opening of 5) blocks it; I_OPEN (axis 77.5, dz -2.5, exactly half the "
        "opening) lets it through to D3.", _iris_in_height, False),
    "height_mixed": (
        "Heights with a split, a Gaussian beam, stacked mirrors, an iris and a periscope. L1 (785 nm, 1.5 mm, 20 mW) at "
        "78 mm, 3 mm above BS1 (75): both branches stay at 78. Transmitted: M_B (300 mm) and M_A (75) stand at one "
        "place; M_A, at the beam's height, is hit, and M_B, at the very point of the hit, is not a pass; the beam goes "
        "up to IRIS (75, opening 4), which blocks it in height (|dz| 3 > 2). Reflected: it passes under M_P (200, dz "
        "-122); P1 (entrance 75, exit 300, along its +x) lifts it to 303 (dz +3 at both ends), a 225 mm run; M_UP "
        "(300) turns it to +x; it crosses the surfaces of M_NM (75) and M_NM2 (300) 7.1 mm beyond their edges, and only "
        "M_NM2, at its height, is a near miss (expected_misses); D_S (300, aperture 6) is hit with dz +3 = half its "
        "aperture, path_mm 1375, d = 1.757676 mm (zR = 2251.141 mm): its spot reaches 0.878838 mm past its upper edge.",
        _mixed, True),
}


# ── the checks ───────────────────────────────────────────────────────────────────────────────


class Watch:
    """Patch the tracer's rounding and its polarization decisions to see the unrounded numbers."""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.where = ""

    def _tie(self, v, decimals: int, what: str) -> None:
        if v is None:
            return
        x = abs(v) * 10 ** decimals
        if abs(x - math.floor(x) - 0.5) < 1e-6:
            self.problems.append(f"{self.where}: {what} {v!r} is within a millionth of a rounding tie")

    def __enter__(self) -> "Watch":
        self.saved = {name: getattr(T, name)
                      for name in ("_r3", "_r6", "_rmw", "_split", "_polarized", "_pbs_outputs", "_passes")}
        r3, r6, rmw, split = self.saved["_r3"], self.saved["_r6"], self.saved["_rmw"], self.saved["_split"]
        polarized, pbs_outputs, passes = self.saved["_polarized"], self.saved["_pbs_outputs"], self.saved["_passes"]

        def w_r3(v):
            self._tie(v, 3, "a number rounded to 0.001")
            return r3(v)

        def w_r6(v):
            self._tie(v, 6, "a fraction rounded to 1e-6")
            return r6(v)

        def w_rmw(v):
            if T.MW_DECIMALS <= 6:
                self._tie(v, T.MW_DECIMALS, "a power in mW")
            return rmw(v)

        def w_split(c, fraction, st):
            out = split(c, fraction, st)
            if c.type == "pbs":
                for f, _ in out:
                    if f > 0 and abs(f / T.MIN_FRACTION - 1.0) < 1e-6:
                        self.problems.append(f"{self.where}: a PBS branch of fraction {f!r} at the 1e-3 floor")
            return out

        def dark(k, s0, what):
            if s0 > 0 and T.EPS_DARK / 10 < k / s0 < T.EPS_DARK * 10:
                self.problems.append(f"{self.where}: {what} keeps {k / s0!r} of the light, near the dark threshold")

        def w_polarized(st, theta):
            c, s = math.cos(2.0 * theta), math.sin(2.0 * theta)
            dark(0.5 * (st[0] + c * st[1] + s * st[2]), st[0], "a polarizer")
            return polarized(st, theta)

        def w_pbs_outputs(st):
            dark(0.5 * (st[0] - st[1]), st[0], "a PBS (reflected)")
            dark(0.5 * (st[0] + st[1]), st[0], "a PBS (transmitted)")
            return pbs_outputs(st)

        def close(a: float, b: float) -> bool:
            """Neither at one point nor clearly apart: the last bits of a double could decide."""
            return 1e-10 < abs(a - b) < 1e-6

        def w_passes(passed, end_t, inclusive, budget):
            # the meetings that matter: those the segment could record, and the hit (beyond it nothing is recorded)
            reach = end_t + 1e-6 if inclusive else end_t + T.EPS_TIE + 1e-6
            ts = sorted(p[0] for p in passed if p[0] < reach)
            if any(close(a, b) for a, b in zip(ts, ts[1:])):
                self.problems.append(f"{self.where}: two passes of one segment are almost, not exactly, at one point")
            if inclusive:           # the end of a segment that leaves the table
                if any(abs(p[0] - end_t) < 1e-6 for p in passed):
                    self.problems.append(f"{self.where}: a pass at the end of a segment that leaves the table")
            elif any(close(p[0], end_t + T.EPS_TIE) for p in passed):
                self.problems.append(f"{self.where}: a pass almost, not exactly, at the hit that ends its segment")
            return passes(passed, end_t, inclusive, budget)

        T._r3, T._r6, T._rmw, T._split = w_r3, w_r6, w_rmw, w_split
        T._polarized, T._pbs_outputs, T._passes = w_polarized, w_pbs_outputs, w_passes
        return self

    def __exit__(self, *exc) -> None:
        for name, fn in self.saved.items():
            setattr(T, name, fn)


def _newline(path: Path) -> str:
    probe = path if path.exists() else HERE / "README.md"
    return "\r\n" if b"\r\n" in probe.read_bytes() else "\n"


def build(watch: Watch) -> dict[Path, str]:
    """The text every fixture file should have (LF line endings)."""
    out: dict[Path, str] = {}
    names = {p.stem for p in HERE.glob("*.json") if p.name not in NOT_TRACES} | set(BEAM) | set(HEIGHTS)
    for name in sorted(names):
        path = HERE / f"{name}.json"
        watch.where = name
        if name in BEAM:
            description, make = BEAM[name]
            doc = {"name": name, "description": description, "layout": make(), "expected_trace": []}
        elif name in HEIGHTS:
            description, make, misses = HEIGHTS[name]
            doc = {"name": name, "description": description, "layout": make(), "expected_trace": []}
            if misses:
                doc["expected_misses"] = []
        else:
            doc = json.loads(path.read_text(encoding="utf-8"))
        budget = doc.get("trace_options", {}).get("max_trace_events")
        doc["expected_trace"] = T.trace_layout(doc["layout"], max_trace_events=budget)
        if "expected_misses" in doc:
            doc["expected_misses"] = T.near_misses(doc["layout"], doc["expected_trace"])
        out[path] = json.dumps(doc, indent=2) + "\n"
    return out


def main(argv: list[str]) -> int:
    check = "--check" in argv
    with Watch() as watch:
        texts = build(watch)
    if watch.problems:
        print("refusing to write fixtures the two implementations could read apart:")
        for p in watch.problems:
            print("  " + p)
        return 2
    changed = []
    for path, text in texts.items():
        old = path.read_bytes().decode("utf-8").replace("\r\n", "\n") if path.exists() else None
        if old == text:
            continue
        changed.append(path.name)
        if not check:
            with open(path, "w", encoding="utf-8", newline=_newline(path)) as fh:
                fh.write(text)
    print(f"{len(texts)} fixtures, {len(changed)} {'differ' if check else 'written'}: {', '.join(changed) or '-'}")
    return 1 if check and changed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
