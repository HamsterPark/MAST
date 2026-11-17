"""What the MCP server tells a model about the generic station, and the guide page behind it.

The generic station (``docs/v2/design/generic_station.md``) has neither an STM nor a WITec. The
model learns which station it is on from the instructions and ``mast_briefing`` only, so pinned
here: the instructions name the generic station and its guide page, the briefing schema offers
the generic station's sections, and the guide page says only what the code does (a field it
names is a field ``observe`` returns, a code it names is a code the service raises).
"""
from __future__ import annotations

# ── path setup BEFORE any mast.* imports (same bootstrap as test_mast_mcp_optical_station) ──
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

GUIDE_EN = _REPO / "docs" / "external" / "en" / "11-generic-station.md"
GUIDE_ZH = _REPO / "docs" / "external" / "zh" / "11-generic-station.md"


# ── the instructions ────────────────────────────────────────────────────────────
def test_the_first_line_names_the_generic_station():
    first = tools.INSTRUCTIONS.splitlines()[0]
    assert "generic station" in first and "optical station" in first


def test_the_station_rule_covers_the_generic_station_and_names_its_guide():
    rule = next(l for l in tools.INSTRUCTIONS.splitlines() if l.startswith("15. "))
    assert "generic" in rule and "11-generic-station.md" in rule
    assert "mast://guide/en/09-optical-station.md" in rule, "the optical guide stays where it was"


def test_the_rule_numbers_did_not_move():
    numbers = [int(m.group(1)) for m in re.finditer(r"^(\d+)\. ", tools.INSTRUCTIONS, re.M)]
    assert numbers == list(range(1, len(numbers) + 1)) and len(numbers) == 18


def test_the_briefing_tool_offers_the_environment_section():
    from mast.api.ext.overview import GENERIC_ORDER

    assert "environment" in tools.BRIEFING_SECTIONS
    assert set(GENERIC_ORDER) <= set(tools.BRIEFING_SECTIONS)
    assert "generic station" in tools.TOOLS["mast_briefing"].description


def test_the_ask_operator_tool_says_structured_prompts_exist_on_the_generic_station():
    assert "generic" in tools.TOOLS["mast_ask_operator"].description


# ── the guide resource ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("lang", ["en", "zh"])
def test_the_generic_station_guide_is_served_as_a_resource(lang):
    res = GuideResources(str(_REPO / "integrations" / "claude-code" / "skills" / "mast-operator"
                             / "references"))
    uri = f"mast://guide/{lang}/11-generic-station.md"
    assert uri in {r["uri"] for r in res.list_resources()}
    body = res.read_resource(uri)["text"]
    assert body == (GUIDE_EN if lang == "en" else GUIDE_ZH).read_text(encoding="utf-8")


def _tokens(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {t for t in re.findall(r"`([^`\n]+)`", text) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", t)}


def test_both_languages_name_the_same_identifiers():
    en, zh = _tokens(GUIDE_EN), _tokens(GUIDE_ZH)
    assert en == zh, f"only in en: {sorted(en - zh)}; only in zh: {sorted(zh - en)}"


def test_every_field_of_the_echo_table_is_a_field_the_echo_returns():
    from mast.vista import state

    src = Path(state.__file__).read_text(encoding="utf-8")
    section = GUIDE_EN.read_text(encoding="utf-8").split("`instrument` is the state echo", 1)[1]
    table = section.split("\n## ", 1)[0]
    named = {t for t in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", table)}
    assert {"environment", "devices_closed", "optical_state", "operator_prompts_open"} <= named
    for key in named - {"value", "unit", "status", "age_s"}:
        assert f'"{key}"' in src or f"'{key}'" in src or f"{key}" in src, key
    from mast import station_view

    sv_src = Path(station_view.__file__).read_text(encoding="utf-8")
    for key in ("value", "unit", "status", "age_s"):
        assert f'"{key}"' in sv_src, key


def test_the_codes_and_names_the_guide_uses_exist():
    import mast.vista.service as service
    from mast import station
    from mast.api.ext import jobs

    guide = GUIDE_EN.read_text(encoding="utf-8")
    assert "`not_available_on_this_station`" in guide
    assert '"not_available_on_this_station"' in Path(service.__file__).read_text(encoding="utf-8")
    assert "`skill_disabled`" in guide and '"skill_disabled"' in Path(jobs.__file__).read_text(encoding="utf-8")
    assert f"`{station.SERIAL_AUTODETECT_KEY}`" in guide
    assert "`station_capabilities`" in guide
    assert "`laser_area_safe`" in guide


def test_the_guide_index_lists_the_page():
    for lang in ("en", "zh"):
        readme = (_REPO / "docs" / "external" / lang / "README.md").read_text(encoding="utf-8")
        assert "11-generic-station.md" in readme
