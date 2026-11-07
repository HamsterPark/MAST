# -*- coding: utf-8 -*-
"""English text for the factory composite twins is applied at read time, never stored.

The twins in ``builtin_composites.py`` are fingerprinted (``factory_content_hash`` tells an untouched
factory copy from an edited one) and persisted in the version store, so their Chinese text cannot be
wrapped in ``tr()``: the fingerprint would then depend on the language of whoever seeded the store.
``builtin_composites_i18n`` keeps a ``{factory Chinese text: English}`` table instead, and
``localize_composite_dict`` swaps a string only when the language is English and the string is exactly
a factory text.

These tests pin the table to the factory specs in both directions (it cannot rot), the behaviour in
each language, that an edited text is left alone, and that the fingerprint is untouched.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from mast.i18n import use_lang
from mast.skills.composite import builtin_composites as bc
from mast.skills.composite.builtin_composites_i18n import EN_BY_ZH, localize_composite_dict
from mast.skills.composite.spec import safe_eval

_CJK = re.compile(r"[　-〿一-鿿＀-￯]")


def _strings(x, out=None):
    """Every string VALUE in a nested dict / list (dict keys are not text)."""
    out = [] if out is None else out
    if isinstance(x, str):
        out.append(x)
    elif isinstance(x, list):
        for v in x:
            _strings(v, out)
    elif isinstance(x, dict):
        for v in x.values():
            _strings(v, out)
    return out


def _factory_dicts() -> list[dict]:
    return [s.to_dict() for s in bc.reconstructed_composites()]


def _factory(name: str) -> dict:
    return next(d for d in _factory_dicts() if d["name"] == name)


def _only_factory_texts_changed(before, after, path="spec"):
    """``after`` has the shape of ``before``; the only leaves that differ are factory texts, and
    each of those holds exactly its English entry."""
    if isinstance(before, dict):
        assert isinstance(after, dict) and list(before) == list(after), path
        for k in before:
            _only_factory_texts_changed(before[k], after[k], f"{path}.{k}")
    elif isinstance(before, list):
        assert isinstance(after, list) and len(before) == len(after), path
        for i, (a, b) in enumerate(zip(before, after)):
            _only_factory_texts_changed(a, b, f"{path}[{i}]")
    elif isinstance(before, str) and before in EN_BY_ZH:
        assert after == EN_BY_ZH[before], path
    else:
        assert after == before and type(after) is type(before), path


# ── the table cannot rot ──────────────────────────────────────────────────────


def test_every_key_is_a_literal_that_occurs_in_builtin_composites():
    src = Path(bc.__file__).read_text(encoding="utf-8")
    literals = {n.value for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    missing = [k for k in EN_BY_ZH if k not in literals]
    assert not missing, (
        "these keys are not a literal in builtin_composites.py any more (the text was edited or "
        f"removed there; update the table): {missing}")


def test_the_table_is_exactly_the_chinese_text_of_the_factory_specs():
    texts = {s for d in _factory_dicts() for s in _strings(d) if _CJK.search(s)}
    assert len(texts) > 40, "found almost no Chinese text in the factory specs; this check would be vacuous"
    missing = sorted(texts - set(EN_BY_ZH))
    dead = sorted(set(EN_BY_ZH) - texts)
    assert not missing, f"Chinese text in a factory spec without an English entry: {missing}"
    assert not dead, f"table entries that no factory spec contains any more: {dead}"


def test_english_values_are_clean_and_dsl_strings_stay_valid_expressions():
    for zh, en in EN_BY_ZH.items():
        assert en.strip(), zh
        assert not _CJK.search(en), f"Chinese left in the English text of {zh!r}"
        if zh.startswith("'"):
            # a node's reason / value is a DSL expression: the English one must still evaluate to
            # a string (an apostrophe inside would break it)
            assert isinstance(safe_eval(zh, {}), str), zh
            assert isinstance(safe_eval(en, {}), str), en


# ── behaviour per language ────────────────────────────────────────────────────


def test_english_mode_translates_every_factory_spec_and_touches_nothing_else():
    with use_lang("en"):
        for d in _factory_dicts():
            out = localize_composite_dict(d)
            left = [s for s in _strings(out) if _CJK.search(s)]
            assert not left, f"{d['name']}: still Chinese in English mode: {left}"
            assert out["description"] == EN_BY_ZH[d["description"]]
            _only_factory_texts_changed(d, out, d["name"])


def test_the_prescan_twin_messages_are_translated_inside_the_nodes():
    d = _factory("PreScanCheck")
    with use_lang("en"):
        out = localize_composite_dict(d)
    reasons = [s for s in _strings(out["nodes"]) if s.startswith("'")]
    assert any("line_time_s" in r and "declarative" in r for r in reasons), reasons
    assert not any(_CJK.search(s) for s in _strings(out["nodes"]))


def test_chinese_mode_returns_an_equal_independent_copy():
    with use_lang("zh"):
        for d in _factory_dicts():
            out = localize_composite_dict(d)
            assert out == d and out is not d
            before = d["params"][0]["description"]
            out["params"][0]["description"] = "changed on the copy"
            assert d["params"][0]["description"] == before, "the copy shares state with the input"


def test_list_and_version_rows_are_localized_too():
    d = _factory("PreScanCheck")
    row = {"name": d["name"], "version": 3, "description": d["description"], "n_nodes": 12,
           "tags": ["scan", "prescan"]}
    with use_lang("en"):
        assert localize_composite_dict(row) == {**row, "description": EN_BY_ZH[d["description"]]}
    with use_lang("zh"):
        assert localize_composite_dict(row) == row


# ── an edited text is the user's, not ours ────────────────────────────────────


def test_an_edited_value_passes_through_untouched():
    d = _factory("FullScan")
    factory_desc = d["description"]
    d["description"] = factory_desc + "（本机固定 256 px）"           # appended to
    d["params"][0]["description"] = "scan centre X"                   # rewritten
    d["params"][1]["description"] = factory_desc[:-1]                 # only a prefix of a factory text
    assert d["params"][2]["description"] == "宽 (m)"                  # still the factory text
    with use_lang("en"):
        out = localize_composite_dict(d)
    assert out["description"] == d["description"]
    assert out["params"][0]["description"] == "scan centre X"
    assert out["params"][1]["description"] == factory_desc[:-1]
    assert out["params"][2]["description"] == EN_BY_ZH["宽 (m)"]      # untouched ones still translate


# ── fingerprints and stored versions are untouched ────────────────────────────


def test_the_fingerprint_depends_on_neither_the_language_nor_localizing():
    with use_lang("zh"):
        zh_hashes = [bc.factory_content_hash(s) for s in bc.reconstructed_composites()]
    with use_lang("en"):
        specs = bc.reconstructed_composites()
        en_hashes = [bc.factory_content_hash(s) for s in specs]
        for s in specs:
            localize_composite_dict(s.to_dict())                       # reading must not write
        after = [bc.factory_content_hash(s) for s in specs]
    assert zh_hashes == en_hashes == after, (
        "the factory content hash changed with the language: builtin_composites.py must stay free "
        "of tr() (the English lives in builtin_composites_i18n.py)")
