"""Heights in the layout document (contract ``docs/v2/design/optical_layout_3d.md`` §2).

Every component takes ``props.z_mm`` (the height of its optical axis above the table top), a periscope
also ``z_out_mm`` (the height of its exit), the table ``beam_height_mm`` (the height of everything that
gives none; 75 mm when it is not given). All additive: a layout that uses no height is stored byte for
byte as before - above all, no ``"beam_height_mm": null`` appears in it, or an older MAST could no longer
read a layout the moment it is saved again.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from mast.i18n import use_lang
from mast.optical_layout.model import (
    COMMON_PROPS,
    DEFAULT_BEAM_HEIGHT_MM,
    PROPS,
    TYPES,
    Layout,
    beam_height,
    component_z,
    periscope_heights,
    prop_specs,
    uses_heights,
)

from ._layouts import comp, laser, layout, mirror, trace_fixture_files


def ok(components=None, **kw) -> Layout:
    return Layout.model_validate(layout(components if components is not None else [], **kw))


def refused(raw, *, where: str) -> None:
    with use_lang("en"), pytest.raises(ValidationError) as exc:
        Layout.model_validate(raw)
    assert any(where in ".".join(str(p) for p in e["loc"]) or where in e["msg"] for e in exc.value.errors()), exc.value.errors()


def with_table(**table) -> dict:
    raw = layout([])
    raw["table"] = {**raw["table"], **table}
    return raw


# ── z_mm: every type ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("t", sorted(TYPES))
def test_every_type_takes_a_height_rounded_to_a_hundredth_of_a_millimetre(t):
    props = {"wavelength_nm": 532} if t in ("laser", "fiber_out") else {"edge_nm": 600} if t == "dichroic" else {}
    c = ok([comp("c", t, 0, 0, props={**props, "z_mm": 120.004})]).components[0]
    assert c.props["z_mm"] == 120.0
    assert ok([comp("c", t, 0, 0, props={**props, "z_mm": -0.004})]).components[0].props["z_mm"] == 0.0
    assert str(ok([comp("c", t, 0, 0, props={**props, "z_mm": -0.004})]).components[0].props["z_mm"]) == "0.0"
    assert "z_mm" in prop_specs(t)


def test_the_height_has_the_bounds_of_the_contract_and_null_is_not_given():
    assert ok([mirror("m", 0, 0, 0, props={"z_mm": 10000})]).components[0].props == {"z_mm": 10000.0}
    assert ok([mirror("m", 0, 0, 0, props={"z_mm": -10000})]).components[0].props == {"z_mm": -10000.0}
    assert ok([mirror("m", 0, 0, 0, props={"z_mm": None})]).components[0].props == {}
    for bad in (10000.01, -10000.01, "120", True, float("nan"), float("inf"), [120]):
        refused(layout([mirror("m", 0, 0, 0, props={"z_mm": bad})]), where="props")


def test_the_common_props_are_a_table_of_their_own_and_the_type_tables_keep_their_shape():
    """``z_mm`` is in COMMON_PROPS, not copied into every PROPS entry: a type that takes no parameter of its
    own (a PBS, a detector) still has no PROPS entry."""
    assert set(COMMON_PROPS) == {"z_mm"}
    assert "pbs" not in PROPS and "detector" not in PROPS
    assert all("z_mm" not in specs for specs in PROPS.values())
    assert prop_specs("pbs") == COMMON_PROPS


def test_only_a_periscope_has_an_exit_height():
    c = ok([comp("p", "periscope", 0, 0, props={"z_out_mm": 250.006, "z_mm": 75})]).components[0]
    assert c.props == {"z_out_mm": 250.01, "z_mm": 75.0}
    # no cross check: the exit height alone is fine (the entrance is then at the table's beam height)
    assert ok([comp("p", "periscope", 0, 0, props={"z_out_mm": 250})]).components[0].props == {"z_out_mm": 250.0}
    for t in sorted(set(TYPES) - {"periscope"}):
        props = {"wavelength_nm": 532} if t in ("laser", "fiber_out") else {"edge_nm": 600} if t == "dichroic" else {}
        refused(layout([comp("c", t, 0, 0, props={**props, "z_out_mm": 250})]), where="props")
    for bad in (10000.01, "250", False):
        refused(layout([comp("p", "periscope", 0, 0, props={"z_out_mm": bad})]), where="props")


def test_an_unknown_key_names_the_height_among_the_allowed_ones():
    with use_lang("en"), pytest.raises(ValidationError) as exc:
        Layout.model_validate(layout([comp("p", "pbs", 0, 0, props={"reflect_fraction": 0.5})]))
    assert "allowed: z_mm" in str(exc.value)


# ── table.beam_height_mm ─────────────────────────────────────────────────────────────────────


def test_a_table_without_a_beam_height_is_stored_without_the_key():
    for raw in (layout([]), with_table(beam_height_mm=None)):
        stored = Layout.model_validate(raw).to_dict()
        assert "beam_height_mm" not in stored["table"]
        assert set(stored["table"]) == {"width_mm", "height_mm", "hole_pitch_mm", "hole_offset_mm", "grid"}
        assert "beam_height_mm" not in json.dumps(stored)


def test_a_beam_height_is_rounded_kept_and_reads_back_the_same():
    lay = Layout.model_validate(with_table(beam_height_mm=120.004))
    stored = lay.to_dict()
    assert stored["table"]["beam_height_mm"] == 120.0
    assert Layout.model_validate(stored).to_dict() == stored                     # normalising twice changes nothing
    assert Layout.model_validate(with_table(beam_height_mm=10000)).table.beam_height_mm == 10000.0
    assert Layout.model_validate(with_table(beam_height_mm=0.005)).table.beam_height_mm == 0.01


@pytest.mark.parametrize("bad", [0, -1, 0.004, 10000.01, True, "x", float("inf"), float("nan")])
def test_a_beam_height_out_of_range_is_refused_not_cut(bad):
    refused(with_table(beam_height_mm=bad), where="beam_height_mm")


def test_the_schema_has_the_beam_height_as_an_optional_number():
    """The openapi of the API is made from these models: one more optional field, nothing else changes, and the
    validation and serialisation schemas are the same (FastAPI would otherwise split Table into two)."""
    for mode in ("validation", "serialization"):
        props = Layout.model_json_schema(mode=mode)["$defs"]["Table"]["properties"]
        assert set(props) == {"width_mm", "height_mm", "hole_pitch_mm", "hole_offset_mm", "grid", "beam_height_mm"}
        assert props["beam_height_mm"]["anyOf"] == [{"type": "number"}, {"type": "null"}]
    assert Layout.model_json_schema(mode="validation")["$defs"]["Table"] == Layout.model_json_schema(mode="serialization")["$defs"]["Table"]


def test_the_shared_fixtures_layouts_that_use_no_height_are_stored_as_before():
    """A fixture's layout is canonical (the stored form); none of the v1 / v2 ones names a height, and the
    canonical form of each still has exactly the keys it had."""
    for path in trace_fixture_files():
        raw = json.loads(path.read_text(encoding="utf-8"))["layout"]
        if path.stem.startswith("height_"):
            continue
        assert Layout.model_validate(raw).to_dict() == raw, path.name
        assert not uses_heights(Layout.model_validate(raw)), path.name


# ── reading the heights ──────────────────────────────────────────────────────────────────────


def test_the_heights_are_read_with_their_defaults():
    lay = ok([mirror("m", 0, 0, 0), mirror("hi", 0, 0, 0, props={"z_mm": 200}),
              comp("p", "periscope", 0, 0), comp("q", "periscope", 0, 0, props={"z_out_mm": 250}),
              comp("r", "periscope", 0, 0, props={"z_mm": 60, "z_out_mm": 300})])
    h = beam_height(lay.table)
    assert h == DEFAULT_BEAM_HEIGHT_MM == 75.0
    m, hi, p, q, r = lay.components
    assert (component_z(m, h), component_z(hi, h)) == (75.0, 200.0)
    assert periscope_heights(p, h) == (75.0, 75.0)          # no exit height: the entrance's
    assert periscope_heights(q, h) == (75.0, 250.0)
    assert periscope_heights(r, h) == (60.0, 300.0)
    lay2 = Layout.model_validate({**layout([mirror("m", 0, 0, 0)]), "table": {"beam_height_mm": 120}})
    assert beam_height(lay2.table) == 120.0 and component_z(lay2.components[0], beam_height(lay2.table)) == 120.0


def test_a_layout_uses_heights_when_it_names_one_anywhere():
    assert not uses_heights(ok([mirror("m", 0, 0, 0), laser("l", 0, 0)]))
    assert uses_heights(ok([mirror("m", 0, 0, 0, props={"z_mm": 75})]))           # even at the default
    assert uses_heights(ok([comp("p", "periscope", 0, 0, props={"z_out_mm": 75})]))
    assert uses_heights(Layout.model_validate(with_table(beam_height_mm=75)))
