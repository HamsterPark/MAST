"""The optical-layout guide (``docs/external/{en,zh}/10-optical-layout.md``) says only what the code does.

The page tells an outside agent how to read the operator's drawing, what the trace means and what it
may and may not do. Each claim that can be checked is: the identifiers are the same in both languages,
the tools, endpoints, error codes, actions and limits it names are the ones in the code, every
component type is covered, and the sample description is exactly what ``summarize`` produces.
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
import typing

import pytest

EN = _REPO / "docs" / "external" / "en" / "10-optical-layout.md"
ZH = _REPO / "docs" / "external" / "zh" / "10-optical-layout.md"
GUIDES = {"en": EN, "zh": ZH}


def text(lang: str) -> str:
    return GUIDES[lang].read_text(encoding="utf-8")


def tokens(path: Path) -> set[str]:
    return {t for t in re.findall(r"`([^`\n]+)`", path.read_text(encoding="utf-8")) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", t)}


def test_both_languages_exist_and_name_the_same_identifiers():
    assert EN.is_file() and ZH.is_file()
    en, zh = tokens(EN), tokens(ZH)
    assert en == zh, f"only in en: {sorted(en - zh)}; only in zh: {sorted(zh - en)}"
    assert len(en) > 40, "the page names too little to check anything"


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_the_readme_lists_the_page(lang):
    readme = (_REPO / "docs" / "external" / lang / "README.md").read_text(encoding="utf-8")
    assert "[10-optical-layout.md](10-optical-layout.md)" in readme


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_the_tools_the_page_names_exist_and_it_names_both(lang):
    from mast_mcp.tools import TOOLS

    named = set(re.findall(r"`(mast_[a-z_]+)`", text(lang)))
    assert {"mast_optical_layout", "mast_optical_layout_comment", "mast_briefing", "mast_ask_operator", "mast_wait"} <= named
    assert named <= set(TOOLS), named - set(TOOLS)


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_the_endpoints_and_the_briefing_section_it_names_exist(lang):
    from types import SimpleNamespace

    from mast.api.ext.app import create_ext_app
    from mast.api.ext.jobs import JobManager
    from mast.api.ext.overview import SECTIONS

    paths = set(create_ext_app(SimpleNamespace(), job_manager=JobManager("unused")).openapi()["paths"])
    named = set(re.findall(r"`GET (/optical-layouts[^`]*)`", text(lang))) | set(re.findall(r"`POST (/optical-layouts[^`]*)`", text(lang)))
    assert named and named <= paths, named - paths
    assert "optical_layouts" in SECTIONS and "`optical_layouts`" in text(lang)


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_the_error_codes_it_names_are_exactly_the_ones_the_surface_raises(lang):
    src = (_REPO / "MASTv2" / "mast" / "api" / "ext" / "optical_layout.py").read_text(encoding="utf-8")
    store = (_REPO / "MASTv2" / "mast" / "optical_layout" / "store.py").read_text(encoding="utf-8")
    raised = set(re.findall(r'ExtError\(\d+, "([a-z_]+)"', src)) | set(re.findall(r'"(too_many_open_comments|layout_comments_full)"', store))
    named = set(re.findall(r"^\| `([a-z_]+)` \| (?:404|422|429|500|503) \|", text(lang), re.MULTILINE))
    assert {"unknown_layout", "layout_unreadable", "invalid_comment", "too_many_open_comments", "layout_comments_full"} <= named
    assert named <= raised | {"exceptions"}, (named, raised)
    # what the surface can raise that matters to a reader is on the page
    assert {"unknown_layout", "layout_unreadable", "invalid_comment", "too_many_open_comments", "layout_comments_full"} <= raised


def table_rows(lang: str, first_header: str) -> list[str]:
    """The body rows of the markdown table whose header row starts with *first_header*."""
    lines = text(lang).splitlines()
    head = next(i for i, ln in enumerate(lines) if ln.startswith(f"| {first_header} |"))
    rows = []
    for ln in lines[head + 2:]:
        if not ln.startswith("|"):
            break
        rows.append(ln)
    return rows


@pytest.mark.parametrize("lang, header", [("en", "Value"), ("zh", "取值")])
def test_the_actions_and_ends_it_lists_are_the_ones_the_trace_produces(lang, header):
    from mast.api.schemas_optical_layout import TraceBranch, TraceEvent

    actions = set(typing.get_args(TraceEvent.model_fields["action"].annotation))
    ends = set(typing.get_args(TraceBranch.model_fields["end"].annotation))
    rows = table_rows(lang, header)
    in_action = set().union(*(set(re.findall(r"`([a-z_]+)`", r.split("|")[1])) for r in rows if "`action`" in r))
    in_end = set().union(*(set(re.findall(r"`([a-z_]+)`", r.split("|")[1])) for r in rows if "`end`" in r))
    assert in_action == actions, (in_action, actions)
    assert in_end == ends, (in_end, ends)


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_the_numbers_it_states_are_the_limits_in_the_code(lang):
    from mast.api.ext.optical_layout import MAX_OPEN_COMMENTS_PER_AGENT
    from mast.optical_layout.store import MAX_COMMENT_CHARS, MAX_COMMENTS
    from mast.optical_layout.summary import MAX_CHARS, OFF_CENTER_SHARE
    from mast.optical_layout.trace import MAX_EVENTS, MAX_TRACE_EVENTS

    body = text(lang)
    assert (MAX_OPEN_COMMENTS_PER_AGENT, MAX_COMMENTS, MAX_COMMENT_CHARS, MAX_CHARS, MAX_EVENTS, MAX_TRACE_EVENTS) == (50, 1000, 2000, 8000, 64, 5000)
    assert OFF_CENTER_SHARE == 0.20
    for needle in ("50 ", "1000 ", "2000 ", "8000 ", "20%", "64 ", "5000 "):
        assert needle in body, needle


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_every_component_type_is_in_the_conventions_table(lang):
    from mast.optical_layout.model import TYPES

    section = text(lang).split("\n## ")[2]                 # "Conventions" / "约定"
    named = set(re.findall(r"`([a-z_]+)`", section.split("\n\n")[1] if False else section))
    missing = set(TYPES) - named
    assert not missing, f"types the guide does not cover: {sorted(missing)}"


def test_the_sample_description_is_what_summarize_writes():
    """The English sample in both pages is the real output, not a drawing of it."""
    from mast.optical_layout.summary import summarize

    def comp(cid, typ, x, y, angle, label, props=None, **ann):
        c = {"id": cid, "type": typ, "label": label, "x_mm": x, "y_mm": y, "angle_deg": angle, "props": props or {}}
        if ann:
            c["annotations"] = ann
        return c

    comps = [comp("las1", "laser", 50, 62.5, 0, "L1", {"wavelength_nm": 532}, manufacturer="Cobolt", model="Samba"),
             comp("m1", "mirror", 112.5, 62.5, 135, "M1", manufacturer="Thorlabs", model="BB1-E02"),
             comp("hwp1", "waveplate", 112.5, 200, 90, "HWP1", {"retardance": "half"}, motorized=True, actuator="K10CR1", connection="COM12"),
             comp("d", "detector", 112.5, 300, 270, "D1")]
    comps[2]["links"] = {"device_id": "hwp_rot", "slot": "excitation_waveplate"}
    lay = {"schema": 1, "id": "shg", "title": "SHG excitation", "revision": 12, "updated_by": "ana",
           "updated_at": "2026-11-03T12:00:00+00:00", "components": comps}
    links = {"hwp1": {"device": {"id": "hwp_rot", "known": True, "enabled": True, "connected": True},
                      "slot": {"id": "excitation_waveplate", "known": True, "value": "hwp_532", "valid": True}}}
    real = summarize(lay, links=links, lang="en")
    for lang in ("en", "zh"):
        block = re.search(r"```text\n(.*?)```", text(lang), re.DOTALL).group(1)
        assert block == real, f"the sample in the {lang} page drifted from summarize():\n{block}\n---\n{real}"


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_the_page_says_the_agent_cannot_change_or_resolve(lang):
    body = text(lang)
    assert "mast_optical_layout_comment" in body and "mast_ask_operator" in body
    assert ("cannot change it" in body and "resolve a comment" in body) if lang == "en" else ("不能改图" in body and "解决评论" in body)
