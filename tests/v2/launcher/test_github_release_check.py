"""Launcher GitHub releases polling helpers (rate limit + version compare)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_LAUNCHER = _REPO / "mast2_launcher.py"


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location(
        "mast2_launcher_github_releases", _LAUNCHER
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_urls_point_at_public_mast_repo(launcher):
    assert launcher.PUBLIC_RELEASES_URL == "https://github.com/HamsterPark/MAST/releases"
    assert launcher.PUBLIC_ISSUES_URL == "https://github.com/HamsterPark/MAST/issues"
    assert launcher.GITHUB_RELEASES_API.endswith("/HamsterPark/MAST/releases/latest")


def test_version_compare_and_normalize(launcher):
    assert launcher._normalize_release_version("v7.1.4") == "7.1.4"
    assert launcher._is_remote_version_newer("7.1.5", "7.1.4") is True
    assert launcher._is_remote_version_newer("v7.1.4", "7.1.4") is False
    assert launcher._is_remote_version_newer("7.1.4", "7.1.5") is False


def test_should_check_respects_interval(launcher):
    now = 1_700_000_000.0
    fresh = {"checked_at": now - 10}
    assert launcher._should_check_github_release(fresh, now=now, force=False) is False
    assert launcher._should_check_github_release(fresh, now=now, force=True) is False
    assert launcher._should_check_github_release(
        {"checked_at": now - 120}, now=now, force=True
    ) is True
    assert launcher._should_check_github_release(
        {"checked_at": now - launcher.GITHUB_RELEASE_CHECK_MIN_S - 1},
        now=now,
        force=False,
    ) is True
    assert launcher._should_check_github_release({}, now=now, force=False) is True


def test_fetch_ok_and_not_modified(launcher, monkeypatch):
    class _Resp:
        def __init__(self, code, body=None, etag='"abc"'):
            self.status = code
            self.headers = {"ETag": etag}
            self._body = body or {}

        def read(self):
            return json.dumps(self._body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def ok_open(req, timeout=12.0):
        return _Resp(200, {"tag_name": "v7.2.0", "html_url": launcher.PUBLIC_RELEASES_URL})

    monkeypatch.setattr("urllib.request.urlopen", ok_open)
    got = launcher._fetch_latest_github_release()
    assert got["status"] == "ok"
    assert got["version"] == "7.2.0"

    import urllib.error

    def not_mod(req, timeout=12.0):
        raise urllib.error.HTTPError(
            req.full_url, 304, "Not Modified", hdrs=None, fp=None
        )

    monkeypatch.setattr("urllib.request.urlopen", not_mod)
    got2 = launcher._fetch_latest_github_release(etag='"abc"')
    assert got2["status"] == "not_modified"


def test_cache_roundtrip(launcher, tmp_path: Path):
    payload = {"checked_at": 1.0, "version": "7.1.4", "etag": '"x"'}
    launcher._save_github_release_cache(tmp_path, payload)
    assert launcher._load_github_release_cache(tmp_path)["version"] == "7.1.4"
