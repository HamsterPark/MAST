"""Backend response language: English by default, Chinese on request."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast import i18n
from mast.i18n import LanguageMiddleware, tr, use_lang


@pytest.fixture
def no_env(monkeypatch):
    monkeypatch.delenv(i18n.ENV_VAR, raising=False)


def test_default_is_english(no_env):
    assert i18n.get_lang() == "en"
    assert tr("中文", "English") == "English"


def test_env_selects_process_language(monkeypatch):
    monkeypatch.setenv(i18n.ENV_VAR, "zh-CN")
    assert tr("中文", "English") == "中文"


def test_use_lang_overrides_and_restores(no_env):
    with use_lang("zh"):
        assert tr("中文", "English") == "中文"
    assert tr("中文", "English") == "English"
    with use_lang("klingon"):
        assert i18n.get_lang() == "en"


def _app():
    app = FastAPI()

    @app.get("/sync")
    def sync_route():  # runs in the thread pool: the context must follow it
        return {"t": tr("中文", "English")}

    @app.get("/async")
    async def async_route():
        return {"t": tr("中文", "English")}

    app.add_middleware(LanguageMiddleware)
    return TestClient(app)


@pytest.mark.parametrize("path", ["/sync", "/async"])
def test_request_language(no_env, path):
    c = _app()
    assert c.get(path).json()["t"] == "English"
    assert c.get(path, cookies={"mast_lang": "zh"}).json()["t"] == "中文"
    assert c.get(path, headers={"X-MAST-Lang": "zh"}).json()["t"] == "中文"
    # an explicit header beats the browser cookie
    assert c.get(path, cookies={"mast_lang": "zh"},
                 headers={"X-MAST-Lang": "en"}).json()["t"] == "English"
    # the request language does not leak into the process default
    assert i18n.get_lang() == "en"
