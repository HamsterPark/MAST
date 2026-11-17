"""The layout document: what is accepted, what is refused, and the one canonical form it is stored in.

The model is the only door to the disk (the store validates everything it writes and reads), so
this is where the contract's rules of §3 are pinned: normalisation of angles and positions, bounds,
unknown keys, per-type ``props``, ids, counts and the size of the document.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from mast.i18n import use_lang

from mast.optical_layout.model import (
    MAX_COMPONENTS,
    MAX_DOCUMENT_BYTES,
    MAX_SOURCES,
    MAX_TEXTS,
    PROPS,
    SOURCE_TYPES,
    TYPES,
    Layout,
    is_valid_id,
    type_name,
    validate_props,
)

from ._layouts import comp, det, laser, layout, mirror


def ok(components=None, **kw) -> Layout:
    return Layout.model_validate(layout(components if components is not None else [], **kw))


def refused(raw, *, where: str | None = None) -> list[dict]:
    with use_lang("en"), pytest.raises(ValidationError) as exc:      # the suite runs in Chinese
        Layout.model_validate(raw)
    errors = exc.value.errors()
    if where is not None:
        assert any(where in ".".join(str(p) for p in e["loc"]) or where in e["msg"] for e in errors), errors
    return errors


# ── normalisation ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("given, stored", [
    (0, 0.0), (135, 135.0), (-90, 270.0), (360, 0.0), (450, 90.0), (-0.001, 0.0), (359.996, 0.0),
    (359.994, 359.99), (720.5, 0.5), (-720, 0.0), (45.004, 45.0), (45.006, 45.01), (1e5, 280.0),
])
def test_angles_are_brought_into_zero_to_360_and_rounded_to_a_hundredth(given, stored):
    assert ok([mirror("m1", 0, 0, given)]).components[0].angle_deg == pytest.approx(stored, abs=1e-9)
    stored_value = ok([mirror("m1", 0, 0, given)]).components[0].angle_deg
    assert 0.0 <= stored_value < 360.0


def test_normalising_twice_changes_nothing():
    once = ok([mirror("m1", 12.3456, -7.891, -45.678), laser("l", 1.005, 2.675, 719.999)]).to_dict()
    assert Layout.model_validate(once).to_dict() == once


def test_positions_are_rounded_to_a_hundredth_of_a_millimetre_and_negative_zero_is_zero():
    c = ok([mirror("m1", 112.504, -0.004, 0)]).components[0]
    assert (c.x_mm, c.y_mm) == (112.5, 0.0)
    assert json.dumps(c.y_mm) == "0.0", "-0.0 would print as -0.0"


def test_the_aperture_defaults_by_type_and_is_kept_when_given():
    defaults = {t: ok([comp("c", t, 0, 0, props=_needed(t))]).components[0].aperture_mm for t in TYPES}
    assert defaults == {t: i.aperture_mm for t, i in TYPES.items()}
    assert defaults["mirror"] == 25.4 and defaults["objective"] == 10.0 and defaults["laser"] == 2.0
    assert ok([mirror("m", 0, 0, 0, aperture=50.8)]).components[0].aperture_mm == 50.8


def _needed(t):
    return {"laser": {"wavelength_nm": 532}, "fiber_out": {"wavelength_nm": 532},
            "dichroic": {"edge_nm": 700}}.get(t, {})


@pytest.mark.parametrize("bad", [0.0, 0.09, 500.01, -3, float("nan"), float("inf")])
def test_the_aperture_is_between_a_tenth_and_500_millimetres(bad):
    raw = layout([mirror("m", 0, 0, 0)])
    raw["components"][0]["aperture_mm"] = bad
    refused(raw)
    for good in (0.1, 500):
        raw["components"][0]["aperture_mm"] = good
        assert Layout.model_validate(raw).components[0].aperture_mm == good


# ── bounds and unknown keys ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [100_000.01, -100_000.01, float("nan"), float("inf"), "x", True, None])
def test_a_position_must_be_a_finite_number_within_100_metres(value):
    raw = layout([mirror("m", 0, 0, 0)])
    raw["components"][0]["x_mm"] = value
    refused(raw)
    for good in (100_000, -100_000):
        raw["components"][0]["x_mm"] = good
        assert Layout.model_validate(raw).components[0].x_mm == good


def test_unknown_keys_are_refused_at_every_level():
    base = layout([mirror("m", 0, 0, 0)], texts=[{"id": "t1", "x_mm": 1, "y_mm": 2, "text": "hi"}])
    for path in (("zzz",), ("table", "zzz"), ("components", 0, "zzz"), ("components", 0, "annotations", "zzz"),
                 ("components", 0, "links", "zzz"), ("texts", 0, "zzz")):
        raw = json.loads(json.dumps(base))
        if "annotations" in path:
            raw["components"][0]["annotations"] = {}
        if "links" in path:
            raw["components"][0]["links"] = {}
        node = raw
        for p in path[:-1]:
            node = node[p]
        node[path[-1]] = 1
        assert refused(raw), path
    raw = json.loads(json.dumps(base))
    raw["components"][0]["annotations"] = {"extra": [{"key": "a", "value": "b", "zzz": 1}]}
    refused(raw)


def test_the_schema_is_one():
    for bad in (2, 0, "1", None):
        refused({**layout([]), "schema": bad})
    assert ok().to_dict()["schema"] == 1
    with pytest.raises(ValidationError):
        Layout.model_validate({"id": "x", "schema_": 1})


def test_an_unknown_type_is_refused():
    errors = refused(layout([comp("c", "flux_capacitor", 0, 0)]))
    assert errors and "flux_capacitor" in json.dumps(errors, default=str)


def test_the_type_table_and_the_literal_agree_and_name_every_type_in_both_languages():
    for t, info in TYPES.items():
        assert info.zh and info.en and info.aperture_mm > 0
        with use_lang("zh"):
            assert type_name(t) == info.zh
        with use_lang("en"):
            assert type_name(t) == info.en
    assert SOURCE_TYPES == {"laser", "fiber_out", "lamp", "led"}
    assert type_name("nonsense") == "nonsense"


# ── ids ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("ident, good", [
    ("a", True), ("M1", True), ("m_1-b", True), ("x" * 40, True), ("x" * 41, False), ("", False),
    ("a b", False), ("a.b", False), ("a/b", False), ("..", False), ("é", False), ("a\n", False),
    ("con", False), ("NUL", False), ("com1", False), ("LPT9", False), ("com10", True), ("conn", True),
])
def test_ids_are_a_safe_file_name(ident, good):
    assert is_valid_id(ident) is good
    if good:
        assert Layout.model_validate({**layout([]), "id": ident}).id == ident
    else:
        refused({**layout([]), "id": ident})


def test_an_id_is_unique_in_one_layout_across_components_and_texts():
    refused(layout([mirror("m", 0, 0, 0), mirror("m", 5, 5, 0)]), where="twice")
    refused(layout([mirror("a", 0, 0, 0)], texts=[{"id": "a", "x_mm": 1, "y_mm": 1, "text": "x"}]), where="twice")


def test_the_component_id_pattern_is_the_same_as_the_layout_id_pattern():
    refused(layout([mirror("bad id", 0, 0, 0)]))
    refused(layout([mirror("x" * 41, 0, 0, 0)]))


# ── props ────────────────────────────────────────────────────────────────────────────────────


def test_every_type_accepts_empty_props_unless_it_needs_something():
    needs = {"laser", "fiber_out", "dichroic"}
    for t in TYPES:
        if t in needs:
            refused(layout([comp("c", t, 0, 0)]), where="props")
        else:
            assert ok([comp("c", t, 0, 0)]).components[0].props == {}


def test_props_keys_are_checked_against_the_type():
    for t, key in (("mirror", "reflect_fraction"), ("beam_splitter", "edge_nm"), ("lens", "od"),
                   ("detector", "closed"), ("laser", "colour")):
        props = {"wavelength_nm": 532} if t == "laser" else {}
        refused(layout([comp("c", t, 0, 0, props={**props, key: 1})]), where="props")
    assert ok([comp("c", "beam_splitter", 0, 0, props={"reflect_fraction": 0.3})]).components[0].props == {"reflect_fraction": 0.3}


def test_a_source_has_one_form_of_wavelength():
    refused(layout([comp("c", "laser", 0, 0, props={})]), where="wavelength")
    refused(layout([comp("c", "laser", 0, 0, props={"wavelength_nm": 532, "wavelengths_nm": [532]})]), where="wavelength")
    refused(layout([comp("c", "laser", 0, 0, props={"wavelengths_nm": []})]))
    refused(layout([comp("c", "laser", 0, 0, props={"wavelengths_nm": [532, 532]})]), where="duplicate")
    refused(layout([comp("c", "laser", 0, 0, props={"wavelengths_nm": list(range(400, 417))})]))
    assert ok([laser("c", 0, 0, wl=[532, 1064])]).components[0].props == {"wavelengths_nm": [532.0, 1064.0]}
    # lamps and LEDs may have none
    assert ok([comp("c", "lamp", 0, 0)]).components[0].props == {}


@pytest.mark.parametrize("t, props", [
    ("laser", {"wavelength_nm": 0}), ("laser", {"wavelength_nm": -5}), ("laser", {"wavelength_nm": 1e7 + 1}),
    ("laser", {"wavelength_nm": "532"}), ("laser", {"wavelength_nm": True}), ("laser", {"wavelength_nm": float("nan")}),
    ("laser", {"wavelength_nm": 532, "power_mw": -1}),
    ("beam_splitter", {"reflect_fraction": 1.01}), ("beam_splitter", {"reflect_fraction": -0.01}),
    ("dichroic", {"edge_nm": 700, "reflects": "middle"}), ("dichroic", {"reflects": "short"}),
    ("waveplate", {"retardance": "eighth"}), ("filter", {"pass_min_nm": 600, "pass_max_nm": 500}),
    ("nd", {"od": 21}), ("iris", {"open_mm": 0}), ("iris", {"open_mm": 501}),
    ("shutter", {"closed": "yes"}), ("shutter", {"closed": 1}), ("sample", {"reflects": "no"}),
    ("custom", {"behavior": "explode"}), ("fiber_in", {"continues_at": "bad id"}), ("fiber_in", {"continues_at": 5}),
])
def test_bad_props_are_refused(t, props):
    refused(layout([comp("c", t, 0, 0, props=props)]), where="props")


def test_props_set_to_null_count_as_not_given_and_numbers_are_rounded():
    c = ok([comp("c", "filter", 0, 0, props={"pass_min_nm": 500.00049, "pass_max_nm": None})]).components[0]
    assert c.props == {"pass_min_nm": 500.0}
    assert ok([comp("c", "fiber_in", 0, 0, props={"continues_at": ""})]).components[0].props == {}
    assert ok([comp("c", "fiber_in", 0, 0, props={"continues_at": " fo1 "})]).components[0].props == {"continues_at": "fo1"}


def test_the_prop_table_covers_exactly_the_types_the_contract_gives_props():
    assert set(PROPS) == {"laser", "fiber_out", "lamp", "led", "beam_splitter", "dichroic", "grating",
                          "lens", "objective", "waveplate", "polarizer", "filter", "nd", "iris",
                          "shutter", "sample", "fiber_in", "custom",
                          # v2 §3.2: throughput
                          "mirror", "window", "aotf", "prism", "periscope"}
    with pytest.raises(ValueError):
        validate_props("mirror", "not a dict")


# ── v2: the beam props (contract optical_layout_v2.md §3) ────────────────────────────────────


def test_the_beam_props_go_to_exactly_the_types_of_the_contract():
    throughput = {t for t, specs in PROPS.items() if "throughput" in specs}
    assert throughput == {"mirror", "grating", "lens", "objective", "waveplate", "polarizer", "filter", "window",
                          "aotf", "prism", "periscope", "sample", "custom"}
    for t in ("laser", "fiber_out"):
        assert {"polarization", "pol_angle_deg", "beam_diameter_mm", "m2"} <= set(PROPS[t])
    for t in ("lamp", "led"):           # always unpolarized, no Gaussian beam, no absolute power
        assert set(PROPS[t]) == {"wavelength_nm"}
    assert set(PROPS["fiber_in"]) == {"continues_at", "coupling"}
    assert "pbs" not in PROPS and ok([comp("p", "pbs", 0, 0)]).components[0].props == {}
    refused(layout([comp("p", "pbs", 0, 0, props={"reflect_fraction": 0.5})]), where="props")
    refused(layout([comp("p", "beam_splitter", 0, 0, props={"throughput": 0.5})]), where="props")
    refused(layout([comp("l", "lamp", 0, 0, props={"polarization": "linear"})]), where="props")


def test_the_polarizing_beam_splitter_is_a_splitter_of_its_own():
    info = TYPES["pbs"]
    assert (info.role, info.aperture_mm, info.zh, info.en) == ("splitter", 25.4, "偏振分束器", "polarizing beam splitter")
    with use_lang("en"):
        assert type_name("pbs") == "polarizing beam splitter"


def test_the_beam_props_are_rounded_and_kept():
    props = {"wavelength_nm": 633, "polarization": "linear", "pol_angle_deg": -45.006, "beam_diameter_mm": 1.23456749,
             "m2": 1.0004, "power_mw": 50}
    got = ok([comp("l", "laser", 0, 0, props=props)]).components[0].props
    assert got == {"wavelength_nm": 633.0, "polarization": "linear", "pol_angle_deg": -45.01,
                   "beam_diameter_mm": 1.234567, "m2": 1.0, "power_mw": 50.0}
    assert ok([comp("f", "fiber_in", 0, 0, props={"coupling": 0.12345649})]).components[0].props == {"coupling": 0.123456}
    assert ok([comp("m", "mirror", 0, 0, props={"throughput": 0.9999994})]).components[0].props == {"throughput": 0.999999}
    # null is "not given"
    nulls = {"wavelength_nm": 633, "polarization": None, "pol_angle_deg": None, "beam_diameter_mm": None, "m2": None}
    assert ok([comp("l", "laser", 0, 0, props=nulls)]).components[0].props == {"wavelength_nm": 633.0}
    assert ok([comp("l", "laser", 0, 0, props={"wavelength_nm": 633, "polarization": "unpolarized"})]).components[0].props \
        == {"wavelength_nm": 633.0, "polarization": "unpolarized"}


@pytest.mark.parametrize("t, props", [
    ("laser", {"wavelength_nm": 633, "polarization": "circular"}),
    ("laser", {"wavelength_nm": 633, "polarization": "linear", "pol_angle_deg": 360.5}),
    ("laser", {"wavelength_nm": 633, "polarization": "linear", "pol_angle_deg": "45"}),
    ("laser", {"wavelength_nm": 633, "beam_diameter_mm": 0}),
    ("laser", {"wavelength_nm": 633, "beam_diameter_mm": -1}),
    ("laser", {"wavelength_nm": 633, "beam_diameter_mm": 1000.001}),
    ("laser", {"wavelength_nm": 633, "beam_diameter_mm": 1, "m2": 0.99}),
    ("laser", {"wavelength_nm": 633, "beam_diameter_mm": 1, "m2": 1000.5}),
    ("fiber_in", {"coupling": 1.01}), ("fiber_in", {"coupling": -0.1}),
    ("mirror", {"throughput": 1.5}), ("lens", {"throughput": -0.01}), ("custom", {"throughput": True}),
])
def test_bad_beam_props_are_refused(t, props):
    refused(layout([comp("c", t, 0, 0, props=props)]), where="props")


def test_the_two_props_that_need_another_one_are_refused_without_it():
    """``pol_angle_deg`` only with ``polarization = linear``; ``m2`` only with ``beam_diameter_mm`` (§3.2)."""
    for t in ("laser", "fiber_out"):
        for props in ({"pol_angle_deg": 10}, {"polarization": "unpolarized", "pol_angle_deg": 10}):
            errors = refused(layout([comp("c", t, 0, 0, props={"wavelength_nm": 633, **props})]), where="pol_angle_deg")
            assert any("linear" in e["msg"] for e in errors)
        errors = refused(layout([comp("c", t, 0, 0, props={"wavelength_nm": 633, "m2": 1.5})]), where="m2")
        assert any("beam_diameter_mm" in e["msg"] for e in errors)
        # with the other one they are fine, and a linear source needs no angle (0 deg)
        ok([comp("c", t, 0, 0, props={"wavelength_nm": 633, "polarization": "linear", "pol_angle_deg": 10,
                                       "beam_diameter_mm": 2, "m2": 1.5})])
        ok([comp("c", t, 0, 0, props={"wavelength_nm": 633, "polarization": "linear"})])
    with use_lang("zh"), pytest.raises(ValidationError) as exc:
        Layout.model_validate(layout([comp("c", "laser", 0, 0, props={"wavelength_nm": 633, "m2": 1.5})]))
    assert "只能和" in str(exc.value)


@pytest.mark.parametrize("t, key, tiny", [
    ("laser", "wavelength_nm", 0.0004), ("laser", "beam_diameter_mm", 4e-7), ("iris", "open_mm", 0.0004),
    ("grating", "lines_per_mm", 0.0004),
])
def test_a_number_with_an_open_lower_bound_that_rounds_onto_it_is_refused(t, key, tiny):
    """Normalising twice must change nothing: 0.0004 nm would be stored as 0.0, which the next read refuses
    (and a zero wavelength or waist would be a division by zero in the beam physics)."""
    props = {key: tiny}
    if t == "laser" and key != "wavelength_nm":
        props["wavelength_nm"] = 633
    refused(layout([comp("c", t, 0, 0, props=props)]), where=key)
    props[key] = tiny * 2 if key != "beam_diameter_mm" else 6e-7          # rounds to the smallest step: kept
    c = ok([comp("c", t, 0, 0, props=props)]).components[0]
    assert Layout.model_validate(ok([comp("c", t, 0, 0, props=props)]).to_dict()).components[0].props == c.props


def test_point_0001_nm_is_refused_rather_than_stored_as_zero():
    """The case of the contract (v2 §12.1): before, 0.0001 nm was accepted and stored as 0.0, which the next
    read refused - a layout that could be saved but not opened again."""
    errors = refused(layout([laser("l", 0, 0, wl=0.0001)]), where="wavelength_nm")
    assert any("> 0" in e["msg"] for e in errors)
    refused(layout([laser("l", 0, 0, wl=[532, 0.0001])]), where="wavelengths_nm")
    once = ok([laser("l", 0, 0, wl=0.0006)]).to_dict()
    assert once["components"][0]["props"]["wavelength_nm"] == 0.001
    assert Layout.model_validate(once).to_dict() == once                         # normalising twice changes nothing


# ── annotations, links, texts, table ─────────────────────────────────────────────────────────


def test_annotation_text_is_cleaned_and_never_silently_cut():
    c = ok([mirror("m", 0, 0, 0, model=" BB1-\tE02\n ", manufacturer="Thor\x00labs")]).components[0]
    assert c.annotations.model == "BB1- E02" and c.annotations.manufacturer == "Thorlabs"
    refused(layout([mirror("m", 0, 0, 0, model="x" * 201)]))
    assert ok([mirror("m", 0, 0, 0, model="x" * 200)]).components[0].annotations.model == "x" * 200
    refused(layout([mirror("m", 0, 0, 0, notes="x" * 2001)]))
    notes = ok([mirror("m", 0, 0, 0, notes="line1\r\nline2\x07")]).components[0].annotations.notes
    assert notes == "line1\nline2"


def test_extra_pairs_are_bounded():
    pairs = lambda n: [{"key": f"k{i}", "value": "v"} for i in range(n)]  # noqa: E731
    assert len(ok([mirror("m", 0, 0, 0, extra=pairs(20))]).components[0].annotations.extra) == 20
    refused(layout([mirror("m", 0, 0, 0, extra=pairs(21))]))
    refused(layout([mirror("m", 0, 0, 0, extra=[{"key": "k" * 41, "value": "v"}])]))
    refused(layout([mirror("m", 0, 0, 0, extra=[{"key": "", "value": "v"}])]))
    refused(layout([mirror("m", 0, 0, 0, extra=[{"key": "k", "value": "v" * 201}])]))


def test_the_datasheet_link_is_a_web_address_or_empty():
    for good in ("", "http://example.org/a.pdf", "HTTPS://example.org/a.pdf?x=1"):
        assert ok([mirror("m", 0, 0, 0, datasheet_url=good)]).components[0].annotations.datasheet_url == good
    for bad in ("javascript:alert(1)", "file:///C:/x.pdf", "example.org/a.pdf", "ftp://x/y", "http://a b"):
        refused(layout([mirror("m", 0, 0, 0, datasheet_url=bad)]), where="datasheet_url")


def test_links_are_plain_references_that_need_not_exist():
    c = ok([comp("m", "waveplate", 0, 0, 0)]).components[0]
    assert c.links.device_id is None and c.links.slot is None
    raw = layout([comp("m", "waveplate", 0, 0, 0)])
    raw["components"][0]["links"] = {"device_id": "hwp_rot", "slot": "excitation_waveplate"}
    assert Layout.model_validate(raw).components[0].links.slot == "excitation_waveplate"
    for bad in ("has space", "a" * 65, "x/y"):
        raw["components"][0]["links"] = {"device_id": bad}
        refused(raw)


def test_texts_and_the_table_are_checked():
    t = {"id": "t1", "x_mm": 1.234, "y_mm": 5, "text": " door\n ", "size": 2}
    lay = ok(texts=[t])
    assert lay.texts[0].text == "door" and lay.texts[0].x_mm == 1.23
    for bad in ({**t, "size": 0.01}, {**t, "size": 21}, {**t, "text": "x" * 201}, {**t, "x_mm": 1e6}, {**t, "id": "bad id"}):
        refused(layout([], texts=[bad]))
    for table in ({"width_mm": 9}, {"width_mm": 20001}, {"height_mm": 0}, {"hole_pitch_mm": 0.5},
                  {"hole_pitch_mm": 501}, {"hole_offset_mm": -1}, {"grid": "hex"}):
        refused(layout([], **{"lid": "t"}) | {"table": table})
    for grid in ("metric", "imperial", "none"):
        assert Layout.model_validate({**layout([]), "table": {"grid": grid}}).table.grid == grid
    assert ok().table.width_mm == 1200.0 and ok().table.hole_offset_mm == 12.5


# ── limits ───────────────────────────────────────────────────────────────────────────────────


def test_counts_are_bounded():
    many = [comp(f"c{i}", "window", i, 0, 0) for i in range(MAX_COMPONENTS)]
    assert len(ok(many).components) == MAX_COMPONENTS
    refused(layout(many + [comp("extra", "window", 0, 0, 0)]))
    texts = [{"id": f"t{i}", "x_mm": 0, "y_mm": 0, "text": "x"} for i in range(MAX_TEXTS)]
    assert len(ok(texts=texts).texts) == MAX_TEXTS
    refused(layout([], texts=texts + [{"id": "tx", "x_mm": 0, "y_mm": 0, "text": "x"}]))


def test_at_most_32_light_sources():
    lasers = [laser(f"l{i}", i, 0) for i in range(MAX_SOURCES)]
    assert len(ok(lasers).components) == MAX_SOURCES
    refused(layout(lasers + [laser("l_extra", 0, 0)]), where="sources")
    kinds = [comp(f"s{i}", t, i, 0, props=_needed(t)) for i, t in enumerate(sorted(SOURCE_TYPES))]
    assert len(ok(kinds).components) == 4
    refused(layout(lasers[:31] + [comp("a", "led", 0, 0), comp("b", "lamp", 0, 0)]), where="sources")


def test_the_whole_document_is_at_most_one_megabyte():
    big_text = "é" * 2000                 # two bytes each in UTF-8
    comps = [mirror(f"m{i}", i, 0, 0, notes=big_text, model="é" * 200, manufacturer="é" * 200,
                    function="é" * 200, actuator="é" * 200, connection="é" * 200) for i in range(200)]
    raw = layout(comps)
    errors = refused(raw, where="limit")
    assert any(str(MAX_DOCUMENT_BYTES) in e["msg"] for e in errors)
    assert len(json.dumps(Layout.model_validate(layout(comps[:50])).to_dict(), ensure_ascii=False).encode()) < MAX_DOCUMENT_BYTES


def test_the_server_written_fields_are_accepted_and_kept_as_given():
    lay = Layout.model_validate({**layout([]), "revision": 7, "updated_at": "2026-11-03T12:00:00+00:00", "updated_by": "ana"})
    assert (lay.revision, lay.updated_by) == (7, "ana")
    refused({**layout([]), "revision": -1})
    refused({**layout([]), "revision": 1.5})


def test_the_canonical_dict_has_every_field_in_a_stable_order():
    d = ok([mirror("m", 1, 2, 3)]).to_dict()
    assert list(d) == ["schema", "id", "title", "revision", "updated_at", "updated_by", "table",
                       "components", "texts", "notes"]
    assert list(d["components"][0]) == ["id", "type", "label", "x_mm", "y_mm", "angle_deg", "aperture_mm",
                                        "props", "annotations", "links", "locked"]
    assert list(d["components"][0]["annotations"]) == ["model", "manufacturer", "function", "motorized",
                                                       "actuator", "connection", "datasheet_url", "notes", "extra"]
    json.dumps(d, allow_nan=False)


def test_a_detector_and_a_laser_in_the_same_layout_keep_their_own_props():
    lay = ok([laser("l", 0, 0, wl=532), det("d", 10, 0, 180)])
    assert lay.component("l").props == {"wavelength_nm": 532.0} and lay.component("d").props == {}
    assert lay.component("nobody") is None
