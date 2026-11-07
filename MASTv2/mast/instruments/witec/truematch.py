"""WITEC_TRUEMATCH_ASCII — the text WITec Control returns for a *silent spectrum*.

``MultiComm|SequencerSingleSpectrum|SilentSpectrumAsText`` hands back a whole spectrum as text (Control ≥ 5.2,
docs §3). The vendor's reference parser is ``Spectrum.FromTrueMatchString`` in WITecSDK
(``WITecSDK/Modules/HelperStructs.py``, MIT). What that parser tells us about the format:

* blocks start with a ``[Section]`` line; sections seen: ``[WITEC_TRUEMATCH_ASCII_HEADER]``
  (``Version = 2.0``, anything below 2.0 is refused), ``[XData]`` (one float per line),
  ``[SpectrumHeader]`` (``Title``, ``ExcitationWavelength``, ``SpectrumSize``, ``XDataKind``),
  ``[SampleMetaData]`` (``double Integration_Time = …``, ``int Spectrum_Number = …``) and ``[SpectrumData]``
  (one float per line). ``key = value`` with `` = `` as separator;
* a text can carry several spectra (one ``[SpectrumData]`` block per accumulation —
  ``Accumulations`` counts them and ``AccumulatedSpectrumData`` averages them);
* the SDK drops the **last line** of every ``[SpectrumData]`` block and reads ``SpectrumSize - 1`` columns.
  Whether that line is a blank artefact of the file's final newline or a real trailer value is not visible
  from the code, and **no real sample text was available when this was written**.

So this parser does not copy the "drop one" rule blindly. For every data block it takes the numeric lines and
reconciles the count with the X axis: equal → keep all; one more than the axis → drop the last (and say so in
:attr:`TrueMatchSpectrum.notes`); anything else → :class:`TrueMatchError`. The first real acquisition must be
checked against the notes (``AcquireSpectrum`` stores the raw text next to the ``.npy``).

The X unit is whatever WITec's *Spectral Unit* parameter was set to when the spectrum was measured
(``UserParameters|Spectrograph<n>|SpectralUnit``: nm, µm, 1/cm, rel. 1/cm, eV, meV, rel. eV, rel. meV).
:meth:`TrueMatchSpectrum.axis_nm` converts to wavelength in nm, using the excitation wavelength for the
relative units.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

__all__ = ["TrueMatchError", "TrueMatchSpectrum", "parse_truematch", "format_truematch",
           "HC_EV_NM", "unit_from_kind"]

#: h·c in eV·nm (CODATA 2018: 1239.841984…)
HC_EV_NM = 1239.841984

_SECTION = re.compile(r"^\s*\[(?P<name>[^\[\]]+)\]\s*$")


class TrueMatchError(ValueError):
    """The text is not a WITEC_TRUEMATCH_ASCII spectrum we can interpret safely."""


@dataclass
class TrueMatchSpectrum:
    version: float | None
    x: np.ndarray                              # as written, in ``x_unit``
    frames: list[np.ndarray]                   # one counts array per spectrum (accumulation)
    x_unit: str                                # nm | um | cm-1 | rel_cm-1 | eV | meV | rel_eV | rel_meV
    x_unit_assumed: bool                       # True when XDataKind was missing/unknown and nm was assumed
    x_kind: str | None = None                  # XDataKind exactly as written
    excitation_nm: float | None = None
    integration_time_s: float | None = None
    spectrum_size: int | None = None
    titles: list[str] = field(default_factory=list)
    spectrum_numbers: list[int] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def accumulations(self) -> int:
        return len(self.frames)

    def axis_nm(self) -> np.ndarray:
        """The X axis as wavelength in nm."""
        x = np.asarray(self.x, dtype=np.float64)
        u = self.x_unit
        exc = self.excitation_nm
        if not np.isfinite(x).all():
            raise TrueMatchError("X axis contains non-finite values")
        if u == "nm":
            return x.copy()
        if u == "um":
            return x * 1.0e3
        if u == "cm-1":
            return _inverse(1.0e7, x, "wavenumber")
        if u in ("eV", "meV"):
            return _inverse(HC_EV_NM, x / (1.0e3 if u == "meV" else 1.0), "photon energy")
        if exc is None or not exc > 0:
            raise TrueMatchError(f"X axis is relative ({u}) but the text has no ExcitationWavelength")
        if u == "rel_cm-1":
            return _inverse(1.0e7, 1.0e7 / exc - x, "scattered wavenumber")
        if u in ("rel_eV", "rel_meV"):
            shift = x / (1.0e3 if u == "rel_meV" else 1.0)
            return _inverse(HC_EV_NM, HC_EV_NM / exc - shift, "scattered photon energy")
        raise TrueMatchError(f"cannot convert X unit {u!r} to nm")  # pragma: no cover

    def raman_shift_cm1(self) -> np.ndarray | None:
        """Raman shift ``1e7/λ_exc − 1e7/λ`` in cm⁻¹ (Stokes positive), or ``None`` without an excitation line."""
        if self.excitation_nm is None or not self.excitation_nm > 0:
            return None
        nm = self.axis_nm()
        return 1.0e7 / self.excitation_nm - 1.0e7 / nm

    def mean(self) -> np.ndarray:
        if not self.frames:
            raise TrueMatchError("no spectrum data in the text")
        return np.mean(np.vstack(self.frames), axis=0)


def _inverse(numerator: float, denom: np.ndarray, what: str) -> np.ndarray:
    if not (denom > 0).all():
        raise TrueMatchError(f"{what} must be positive to convert to a wavelength")
    return numerator / denom


# ── unit recognition ───────────────────────────────────────────────────────────────────


def unit_from_kind(kind: str | None) -> tuple[str, bool]:
    """``(unit, assumed)`` from the ``XDataKind`` text. Unknown/missing → ``("nm", True)``: the vendor SDK
    treats XData as nm without looking at the kind, so that is the documented default."""
    if not kind or not kind.strip():
        return "nm", True
    k = kind.casefold()
    rel = bool(re.search(r"\brel\b|\brel\.|relative|shift", k))
    if re.search(r"\bmev\b", k):
        return ("rel_meV" if rel else "meV"), False
    if re.search(r"\bev\b|electron", k):
        return ("rel_eV" if rel else "eV"), False
    if re.search(r"1\s*/\s*cm|cm\s*-\s*1|cm⁻¹|wavenumber|wave number", k) or "raman shift" in k:
        return ("rel_cm-1" if rel else "cm-1"), False
    if re.search(r"µm|μm|\bum\b|micro", k):
        return "um", False
    if re.search(r"\bnm\b|wavelength", k):
        return "nm", False
    return "nm", True


# ── parsing ─────────────────────────────────────────────────────────────────────────────


def _kv(line: str) -> tuple[str, str] | None:
    key, sep, val = line.partition("=")
    if not sep:
        return None
    return key.strip(), val.strip()


def _numbers(lines: Iterable[str], what: str) -> list[float]:
    out: list[float] = []
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        try:
            out.append(float(s))
        except ValueError:
            raise TrueMatchError(f"{what}: not a number: {s[:40]!r}") from None
    return out


def parse_truematch(text: str) -> TrueMatchSpectrum:
    """Parse a WITEC_TRUEMATCH_ASCII text. Raises :class:`TrueMatchError` rather than guess."""
    if not isinstance(text, str) or not text.strip():
        raise TrueMatchError("empty text — WITec returned no spectrum")
    t = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").lstrip("﻿")

    sections: list[tuple[str, list[str]]] = []
    for line in t.split("\n"):
        m = _SECTION.match(line)
        if m:
            sections.append((m.group("name").strip(), []))
        elif sections:
            sections[-1][1].append(line)
        elif line.strip():
            raise TrueMatchError(f"text starts with {line.strip()[:40]!r}, not a [section] header")

    names = [n for n, _ in sections]
    if "WITEC_TRUEMATCH_ASCII_HEADER" not in names:
        raise TrueMatchError("not a WITEC_TRUEMATCH_ASCII text (no [WITEC_TRUEMATCH_ASCII_HEADER] section)")

    version: float | None = None
    x: list[float] | None = None
    spectra: list[dict] = []          # per spectrum: header kv, meta kv, data
    cur: dict | None = None
    notes: list[str] = []

    def new_spectrum() -> dict:
        d = {"header": {}, "meta": {}, "data": None}
        spectra.append(d)
        return d

    for name, lines in sections:
        if name == "WITEC_TRUEMATCH_ASCII_HEADER":
            for ln in lines:
                kv = _kv(ln)
                if kv and kv[0] == "Version":
                    try:
                        version = float(kv[1])
                    except ValueError:
                        raise TrueMatchError(f"unreadable Version {kv[1]!r}") from None
                    if version < 2.0:
                        raise TrueMatchError(f"WITec TrueMatch ASCII Version {version} not supported (need ≥ 2.0)")
        elif name == "XData":
            vals = _numbers(lines, "[XData]")
            if x is None:
                x = vals
            elif vals != x:
                notes.append("a second [XData] section differs from the first; the first is used")
        elif name == "SpectrumHeader":
            cur = new_spectrum()
            for ln in lines:
                kv = _kv(ln)
                if kv:
                    cur["header"][kv[0]] = kv[1]
        elif name == "SampleMetaData":
            if cur is None or cur["data"] is not None:
                cur = new_spectrum()
            for ln in lines:
                kv = _kv(ln)
                if kv:
                    cur["meta"][kv[0].split()[-1]] = kv[1]
        elif name == "SpectrumData":
            if cur is None or cur["data"] is not None:
                cur = new_spectrum()
            cur["data"] = _numbers(lines, "[SpectrumData]")
        # unknown sections are ignored on purpose (a newer WITec Control may add some)

    if not spectra or all(s["data"] is None for s in spectra):
        raise TrueMatchError("the text has no [SpectrumData] section")
    spectra = [s for s in spectra if s["data"] is not None]
    if not x:
        raise TrueMatchError("the text has no [XData] section — cannot build an axis")

    first_hdr = spectra[0]["header"]
    size_hdr: int | None = None
    if "SpectrumSize" in first_hdr:
        try:
            size_hdr = int(float(first_hdr["SpectrumSize"]))
        except ValueError:
            raise TrueMatchError(f"unreadable SpectrumSize {first_hdr['SpectrumSize']!r}") from None
    expected = len(x) if x else size_hdr
    if x and size_hdr is not None and size_hdr not in (len(x), len(x) - 1, len(x) + 1):
        notes.append(f"SpectrumSize={size_hdr} but [XData] has {len(x)} values")

    frames: list[np.ndarray] = []
    dropped = 0
    for i, s in enumerate(spectra):
        data = s["data"]
        if expected is None:
            expected = len(data)
        if len(data) == expected + 1:
            data = data[:-1]
            dropped += 1
        if len(data) != expected:
            raise TrueMatchError(
                f"spectrum #{i} has {len(data)} values but the X axis has {expected}")
        frames.append(np.asarray(data, dtype=np.float64))
    if dropped:
        notes.append(f"{dropped} [SpectrumData] block(s) had one more value than the X axis; the last was dropped "
                     "(the vendor SDK drops the last line of every block)")
    kind = first_hdr.get("XDataKind")
    unit, assumed = unit_from_kind(kind)
    if assumed:
        notes.append(f"XDataKind {kind!r} not recognised; the X axis is assumed to be in nm (the vendor SDK does the same)")

    exc = None
    if first_hdr.get("ExcitationWavelength"):
        try:
            exc = float(first_hdr["ExcitationWavelength"])
        except ValueError:
            raise TrueMatchError(f"unreadable ExcitationWavelength {first_hdr['ExcitationWavelength']!r}") from None
        if not (math.isfinite(exc) and exc > 0):
            exc = None
    for other in spectra[1:]:
        ow = other["header"].get("ExcitationWavelength")
        if ow and exc is not None:
            try:
                if abs(float(ow) - exc) > 0.5:
                    notes.append("spectra in this text carry different ExcitationWavelength values; the first is used")
                    break
            except ValueError:
                pass

    it = None
    sn: list[int] = []
    for s in spectra:
        v = s["meta"].get("Integration_Time")
        if v is not None and it is None:
            try:
                it = float(v)
            except ValueError:
                pass
        v = s["meta"].get("Spectrum_Number")
        if v is not None:
            try:
                sn.append(int(float(v)))
            except ValueError:
                pass

    extra = {k: v for k, v in first_hdr.items()
             if k not in ("Title", "ExcitationWavelength", "SpectrumSize", "XDataKind")}
    for k, v in spectra[0]["meta"].items():
        if k not in ("Integration_Time", "Spectrum_Number"):
            extra[f"meta.{k}"] = v
    return TrueMatchSpectrum(
        version=version, x=np.asarray(x, dtype=np.float64), frames=frames, x_unit=unit,
        x_unit_assumed=assumed, x_kind=kind, excitation_nm=exc, integration_time_s=it,
        spectrum_size=size_hdr, titles=[s["header"].get("Title", "") for s in spectra],
        spectrum_numbers=sn, extra=extra, notes=notes)


# ── writing (the fake WITec Control and the tests) ──────────────────────────────────────


def format_truematch(x: Sequence[float], frames: Sequence[Sequence[float]], *,
                     excitation_nm: float | None = 532.138, integration_time_s: float = 1.0,
                     x_kind: str = "Wavelength [nm]", titles: Sequence[str] | None = None,
                     version: float = 2.0, trailer: str | None = None) -> str:
    """Produce a text in the shape the vendor parser expects: sections separated by one blank line, the text
    ending with a single newline (so the vendor's "drop the last line" removes exactly the empty string after it).

    ``trailer`` = ``None`` writes plain blocks; a string appends one extra line with that content to every
    ``[SpectrumData]`` block (to exercise the "one value more than the X axis" rule)."""
    n = len(x)
    nl = "\n"
    sections: list[str] = [
        nl.join(["[WITEC_TRUEMATCH_ASCII_HEADER]", f"Version = {version}"]),
        nl.join(["[XData]", *(repr(float(v)) for v in x)]),
    ]
    for i, fr in enumerate(frames):
        head = ["[SpectrumHeader]", f"Title = {titles[i] if titles else f'Spectrum {i}'}"]
        if excitation_nm is not None:
            head.append(f"ExcitationWavelength = {excitation_nm}")
        head += [f"SpectrumSize = {n}", f"XDataKind = {x_kind}"]
        sections.append(nl.join(head))
        sections.append(nl.join(["[SampleMetaData]", f"double Integration_Time = {integration_time_s}",
                                 f"int Spectrum_Number = {i}"]))
        data = ["[SpectrumData]", *(repr(float(v)) for v in fr)]
        if trailer is not None:
            data.append(trailer)
        sections.append(nl.join(data))
    return (nl + nl).join(sections) + nl
