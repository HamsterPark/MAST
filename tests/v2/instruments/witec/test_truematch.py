"""WITEC_TRUEMATCH_ASCII parsing (the text of a silent spectrum) and the X-axis unit conversions."""
from __future__ import annotations

import sys
from pathlib import Path

_MASTV2_ROOT = str(Path(__file__).resolve().parents[4] / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]

import numpy as np
import pytest

from mast.instruments.witec.truematch import (HC_EV_NM, TrueMatchError, format_truematch, parse_truematch,
                                               unit_from_kind)

EXC = 532.138
NM = np.linspace(540.0, 560.0, 11)
FRAMES = [np.arange(11, dtype=float) + k * 100 for k in range(3)]


def _text(**kw):
    kw.setdefault("excitation_nm", EXC)
    return format_truematch(NM, FRAMES, integration_time_s=0.5, **kw)


def _vendor_from_trueMatch_string(text: str):
    """WITecSDK 1.4.2 ``Spectrum.FromTrueMatchString`` (MIT), copied as the reference reading of the format."""
    xdata, titles, exc, size, kind, itime, specnum, data, acc = None, [], None, None, None, None, [], [], 0
    for section in text.split('\n\n'):
        seclist = section.split('\n')
        secheader = seclist[0]
        del seclist[0]
        if secheader == '[WITEC_TRUEMATCH_ASCII_HEADER]':
            for sline in seclist:
                lparts = sline.split(' = ')
                if lparts[0] == 'Version' and float(lparts[1]) < 2.0:
                    raise Exception('WITec TrueMatch ASCII Version not supported')
        elif secheader == '[XData]':
            xdata = [float(i) for i in seclist]
        elif secheader == '[SpectrumHeader]':
            for sline in seclist:
                lparts = sline.split(' = ')
                if lparts[0] == 'Title':
                    titles.append(lparts[1])
                elif lparts[0] == 'ExcitationWavelength':
                    exc = float(lparts[1])
                elif lparts[0] == 'SpectrumSize':
                    size = int(lparts[1])
                elif lparts[0] == 'XDataKind':
                    kind = lparts[1]
        elif secheader == '[SampleMetaData]':
            for sline in seclist:
                lparts = sline.split(' = ')
                if lparts[0] == 'double Integration_Time':
                    itime = float(lparts[1])
                elif lparts[0] == 'int Spectrum_Number':
                    specnum.append(int(lparts[1]))
        elif secheader == '[SpectrumData]':
            del seclist[-1]
            data.append([float(i) for i in seclist])
            acc += 1
    return {"xdata": xdata, "titles": titles, "exc": exc, "size": size, "kind": kind, "itime": itime,
            "specnum": specnum, "data": data, "acc": acc}


# ── structure ─────────────────────────────────────────────────────────────────────────────────────

def test_round_trip_several_accumulations():
    tm = parse_truematch(_text())
    assert tm.accumulations == 3 and len(tm.frames) == 3
    assert np.allclose(tm.x, NM) and tm.x_unit == "nm" and not tm.x_unit_assumed
    for got, want in zip(tm.frames, FRAMES):
        assert np.allclose(got, want)
    assert tm.excitation_nm == EXC and tm.integration_time_s == 0.5
    assert tm.spectrum_size == 11 and tm.version == 2.0
    assert tm.titles == ["Spectrum 0", "Spectrum 1", "Spectrum 2"] and tm.spectrum_numbers == [0, 1, 2]
    assert tm.notes == []
    assert np.allclose(tm.axis_nm(), NM)
    assert np.allclose(tm.mean(), np.mean(FRAMES, axis=0))


def test_single_accumulation_agrees_with_the_vendor_parser():
    """For the shape the vendor parser was written against (one spectrum, the text ends with a newline) both
    readings give the same numbers."""
    text = format_truematch(NM, [FRAMES[0]], excitation_nm=EXC, integration_time_s=1.0)
    ref = _vendor_from_trueMatch_string(text)
    ours = parse_truematch(text)
    assert ref["xdata"] == list(ours.x) and ref["exc"] == ours.excitation_nm
    assert ref["itime"] == ours.integration_time_s and ref["acc"] == ours.accumulations == 1
    assert ref["data"][0] == list(ours.frames[0])


def test_every_block_with_a_trailer_value_is_reconciled_with_the_axis():
    """If WITec's blocks carry one extra value (the vendor SDK drops the last line of every block), the
    parser drops it too — and says so."""
    text = _text(trailer="0")
    ref = _vendor_from_trueMatch_string(text)
    tm = parse_truematch(text)
    for got, vendor in list(zip(tm.frames, ref["data"]))[:-1]:       # the vendor's reading of the last block keeps the trailer
        assert list(got) == vendor and len(got) == 11
    assert all(len(f) == 11 for f in tm.frames)
    assert any("last was dropped" in n for n in tm.notes)


def test_windows_line_endings_bom_blank_lines_and_unknown_sections():
    base = _text()
    noisy = "﻿" + base.replace("\n", "\r\n").replace("[SpectrumData]", "\r\n\r\n[SpectrumData]")
    noisy += "\r\n[SomethingNew]\r\nfoo = bar\r\n"
    tm = parse_truematch(noisy)
    assert tm.accumulations == 3 and np.allclose(tm.frames[1], FRAMES[1])


def test_key_value_separator_is_tolerant():
    text = _text().replace(" = ", "=")
    tm = parse_truematch(text)
    assert tm.excitation_nm == EXC and tm.integration_time_s == 0.5


# ── things that must be errors, not guesses ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad,match", [
    ("", "empty"), ("   \n", "empty"), ("hello\n[XData]\n1\n", "not a \\[section\\]"),
    ("[XData]\n1\n2\n", "no \\[WITEC_TRUEMATCH_ASCII_HEADER\\]"),
])
def test_not_a_truematch_text(bad, match):
    with pytest.raises(TrueMatchError, match=match):
        parse_truematch(bad)


def test_old_version_is_refused():
    with pytest.raises(TrueMatchError, match="Version 1.5 not supported"):
        parse_truematch(_text(version=1.5))


def test_missing_pieces():
    text = _text()
    with pytest.raises(TrueMatchError, match="no \\[SpectrumData\\]"):
        parse_truematch(text.split("[SpectrumHeader]")[0])
    no_x = text.replace("[XData]", "[Other]")
    with pytest.raises(TrueMatchError, match="no \\[XData\\]"):
        parse_truematch(no_x)


def test_length_mismatch_between_axis_and_data_is_an_error():
    short = _text().replace("\n3.0\n", "\n", 1)           # drop a value from the first block
    with pytest.raises(TrueMatchError, match="values but the X axis has 11"):
        parse_truematch(short)


def test_non_numeric_value_is_an_error_with_the_offending_text():
    bad = _text().replace("\n3.0\n", "\n3,0\n", 1)
    with pytest.raises(TrueMatchError, match="not a number: '3,0'"):
        parse_truematch(bad)


# ── axis units ─────────────────────────────────────────────────────────────────────────────────────────────

def _text_in(unit_values, kind, exc=EXC):
    return format_truematch(unit_values, [FRAMES[0]], excitation_nm=exc, integration_time_s=1.0, x_kind=kind)


@pytest.mark.parametrize("kind,conv", [
    ("Wavelength [nm]", lambda nm: nm),
    ("Wavelength [µm]", lambda nm: nm / 1e3),
    ("Wavenumber [1/cm]", lambda nm: 1e7 / nm),
    ("Raman Shift [rel. 1/cm]", lambda nm: 1e7 / EXC - 1e7 / nm),
    ("Energy [eV]", lambda nm: HC_EV_NM / nm),
    ("Energy [meV]", lambda nm: HC_EV_NM / nm * 1e3),
    ("Raman Shift [rel. eV]", lambda nm: HC_EV_NM / EXC - HC_EV_NM / nm),
    ("Raman Shift [rel. meV]", lambda nm: (HC_EV_NM / EXC - HC_EV_NM / nm) * 1e3),
])
def test_every_spectral_unit_converts_back_to_the_same_wavelengths(kind, conv):
    tm = parse_truematch(_text_in(conv(NM), kind))
    assert not tm.x_unit_assumed
    assert np.allclose(tm.axis_nm(), NM, rtol=0, atol=1e-6), tm.x_unit


def test_raman_shift_from_any_unit_agrees():
    shift = 1e7 / EXC - 1e7 / NM
    tm = parse_truematch(_text_in(shift, "Raman Shift [rel. 1/cm]"))
    assert np.allclose(tm.raman_shift_cm1(), shift)
    assert parse_truematch(_text_in(NM, "Wavelength [nm]")).raman_shift_cm1() is not None
    assert parse_truematch(_text(excitation_nm=None)).raman_shift_cm1() is None


def test_unknown_axis_kind_is_nm_by_assumption_and_the_note_says_so():
    tm = parse_truematch(_text_in(NM, "Mystery Axis"))
    assert tm.x_unit == "nm" and tm.x_unit_assumed
    assert any("assumed to be in nm" in n for n in tm.notes)


def test_relative_axis_without_excitation_cannot_be_converted():
    tm = parse_truematch(_text_in(1e7 / EXC - 1e7 / NM, "Raman Shift [rel. 1/cm]", exc=None))
    with pytest.raises(TrueMatchError, match="no ExcitationWavelength"):
        tm.axis_nm()


def test_impossible_axis_values_are_errors():
    tm = parse_truematch(_text_in(np.linspace(-5, 5, 11), "Wavenumber [1/cm]"))
    with pytest.raises(TrueMatchError, match="positive"):
        tm.axis_nm()


@pytest.mark.parametrize("kind,unit", [
    (None, "nm"), ("", "nm"), ("Wavelength (nm)", "nm"), ("nm", "nm"), ("Raman shift", "rel_cm-1"),
    ("rel. 1/cm", "rel_cm-1"), ("Wave number [cm-1]", "cm-1"), ("eV", "eV"), ("rel. meV", "rel_meV"),
    ("µm", "um"), ("Energy (meV)", "meV")])
def test_unit_recognition(kind, unit):
    assert unit_from_kind(kind)[0] == unit
