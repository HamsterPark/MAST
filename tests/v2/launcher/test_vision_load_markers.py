"""Launcher vision warm-up marker parsing + legacy-features disk read.

These helpers keep the Tk progress bar from hanging (or showing a hard
failure) when the service intentionally skips vision load — the public
default since legacy_features_enabled is off and vision weights are not
bundled.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# mast2_launcher lives at the repo root; tests add it to path the same way
# other launcher tests do.
import sys

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from mast2_launcher import (  # noqa: E402
    _legacy_features_enabled_from_disk,
    _parse_vision_load_marker,
)


@pytest.mark.parametrize(
    "line,expected",
    [
        ("MAST_VISION_LOAD_START", ("start", "")),
        ("[INFO] mast.service: MAST_VISION_LOAD_DONE", ("done", "")),
        ("MAST_VISION_LOAD_DONE mock", ("done", "mock")),
        ("MAST_VISION_LOAD_FAILED FileNotFoundError: ckpt", ("failed", "FileNotFoundError: ckpt")),
        ("MAST_VISION_LOAD_SKIPPED disabled", ("skipped", "disabled")),
        ("MAST_VISION_LOAD_SKIPPED mock", ("skipped", "mock")),
        ("Uvicorn running on http://127.0.0.1:7862", None),
        ("", None),
    ],
)
def test_parse_vision_load_marker(line, expected):
    assert _parse_vision_load_marker(line) == expected


def test_legacy_features_default_off(tmp_path: Path):
    assert _legacy_features_enabled_from_disk(tmp_path) is False


def test_legacy_features_reads_ui_settings(tmp_path: Path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "ui_settings.json").write_text(
        json.dumps({"legacy_features_enabled": True}), encoding="utf-8"
    )
    assert _legacy_features_enabled_from_disk(tmp_path) is True


def test_legacy_features_corrupt_file_is_off(tmp_path: Path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "ui_settings.json").write_text("{not-json", encoding="utf-8")
    assert _legacy_features_enabled_from_disk(tmp_path) is False
