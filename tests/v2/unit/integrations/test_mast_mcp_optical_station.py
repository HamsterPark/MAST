"""What the MCP server tells a model about stations, and the guide page that backs it.

The server instructions used to open with "MAST drives a real scanning tunneling microscope".
On an optical station that is wrong from the first word, and the model has no other source of
truth about the station type than these instructions and ``mast_briefing``. Two things are
pinned here: the instructions are station-neutral and point at the briefing, and the optical
station guide page says only things the code still does (a field named in the page must be a
field ``observe`` returns; an error code named in the page must be one the service raises).
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports (same bootstrap as test_wrap_skill_minimal) ──
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
_MASTV2_ROOT = str(_REPO / "MASTv2")
if sys.path[0] != _MASTV2_ROOT:
    while _MASTV2_ROOT in sys.path:
        sys.path.remove(_MASTV2_ROOT)
    sys.path.insert(0, _MASTV2_ROOT)
for _name in list(sys.modules):
    if _name == "mast" or _name.startswith("mast."):
        _f = getattr(sys.modules[_name], "__file__", "") or ""
        if "MASTv2" not in _f.replace("\\", "/"):
            del sys.modules[_name]
_SERVER_DIR = _REPO / "integrations" / "claude-code" / "server"
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import re

import pytest

from mast_mcp import tools
from mast_mcp.resources import GuideResources

GUIDE_EN = _REPO / "docs" / "external" / "en" / "09-optical-station.md"
GUIDE_ZH = _REPO / "docs" / "external" / "zh" / "09-optical-station.md"


# ── the instructions ────────────────────────────────────────────────────────────
def test_the_instructions_do_not_assume_an_stm():
    first = tools.INSTRUCTIONS.splitlines()[0]
    assert first.startswith("MAST drives a real instrument station")
    assert "scanning tunneling microscope" in first and "optical station" in first
    assert not first.startswith("MAST drives a real scanning tunneling microscope")


def test_the_first_rule_sends_the_model_to_the_briefing_for_the_station_type():
    rule1 = next(l for l in tools.INSTRUCTIONS.splitlines() if l.startswith("1. "))
    assert "mast_briefing" in rule1 and "which kind of station" in rule1
    assert rule1.index("mast_briefing") < rule1.index("mast_observe")


def test_there_is_a_rule_for_the_optical_station_and_it_names_the_guide():
    rule = next(l for l in tools.INSTRUCTIONS.splitlines() if l.startswith("15. "))
    for needle in ("mast_run", "mast_act", "mast_ask_operator", "mast_wait", "attest",
                   "mast://guide/en/09-optical-station.md"):
        assert needle in rule, needle


def test_the_stm_rules_are_still_there():
    for needle in ("SAFE refuses autonomous tip processing", "WithdrawTip", "One act, one observation",
                   tools._LONG_WAIT_RULE):
        assert needle in tools.INSTRUCTIONS


# ── the briefing sections the schema offers ─────────────────────────────────────
def test_the_briefing_tool_offers_exactly_the_sections_the_service_has():
    from mast.api.ext.overview import OPTICAL_SECTIONS, SECTIONS

    assert set(tools.BRIEFING_SECTIONS) == set(SECTIONS) | set(OPTICAL_SECTIONS)
    desc = tools.TOOLS["mast_briefing"].input_schema["properties"]["sections"]["description"]
    assert all(s in desc for s in tools.BRIEFING_SECTIONS)
    assert "optical station" in tools.TOOLS["mast_briefing"].description


# ── the guide resource ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("lang", ["en", "zh"])
def test_the_optical_station_guide_is_served_as_a_resource(lang):
    res = GuideResources(str(_REPO / "integrations" / "claude-code" / "skills" / "mast-operator"
                             / "references"))
    uri = f"mast://guide/{lang}/09-optical-station.md"
    assert uri in {r["uri"] for r in res.list_resources()}
    body = res.read_resource(uri)["text"]
    assert body == (GUIDE_EN if lang == "en" else GUIDE_ZH).read_text(encoding="utf-8")


# ── the guide says only what the code does ──────────────────────────────────────
def _tokens(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {t for t in re.findall(r"`([^`\n]+)`", text) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", t)}


def test_both_languages_name_the_same_identifiers():
    en, zh = _tokens(GUIDE_EN), _tokens(GUIDE_ZH)
    assert en == zh, f"only in en: {sorted(en - zh)}; only in zh: {sorted(zh - en)}"


def test_every_field_of_the_observe_table_is_a_field_observe_returns(tmp_path):
    from mast.vista import optical

    sys.path.insert(0, str(_REPO / "tests" / "v2" / "station"))
    try:
        from _spectra import write_spectrum
    finally:
        sys.path.pop(0)
    write_spectrum(tmp_path, saturate=True)
    summary = optical.summarize(optical.newest_spectrum(roots=[tmp_path]))
    returned = set(summary) | {k for p in summary["peaks"] for k in p}
    section = GUIDE_EN.read_text(encoding="utf-8").split("## What `mast_observe` returns", 1)[1]
    table = section.split("`instrument` is the state echo", 1)[0]
    named = {t for t in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", table)}
    named.discard("latest_frame")
    assert named, "the table is empty - the section was renamed?"
    assert named <= returned, f"named in the guide but not returned by observe: {sorted(named - returned)}"
    assert str(int(optical.DEFAULT_SATURATION_COUNTS)) in table


def test_the_state_echo_keys_in_the_guide_are_the_echo_keys():
    from mast.vista import state

    src = (Path(state.__file__)).read_text(encoding="utf-8")
    section = GUIDE_EN.read_text(encoding="utf-8").split("`instrument` is the state echo", 1)[1]
    para = section.split("\n## ", 1)[0]
    for key in ("microscope", "lasers", "temperature", "power_meter", "devices_closed",
                "optical_state", "operator_prompts_open"):
        assert f"`{key}`" in para, key
        assert key in src, key


def test_the_error_codes_the_guide_names_are_codes_the_service_raises():
    import mast.vista.service as service

    guide = GUIDE_EN.read_text(encoding="utf-8")
    src = Path(service.__file__).read_text(encoding="utf-8")
    for code in ("not_available_on_this_station",):
        assert f"`{code}`" in guide and f'"{code}"' in src
    # skill_disabled comes from the job door
    import mast.api.ext.jobs as jobs

    assert "`skill_disabled`" in guide and '"skill_disabled"' in Path(jobs.__file__).read_text(encoding="utf-8")


def test_the_guide_does_not_describe_the_stm_as_the_only_station():
    assert "optical station" in GUIDE_EN.read_text(encoding="utf-8").lower()
    readme = (_REPO / "docs" / "external" / "en" / "README.md").read_text(encoding="utf-8")
    assert "09-optical-station.md" in readme
