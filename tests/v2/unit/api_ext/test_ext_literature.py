"""``POST /literature/search``（``mast.api.ext.literature``）：就是主 API 那条检索的直通。

检索后端用替身（``literature_index.search``）：**从不**调 DashScope，``MAST_LIT_LIVE`` 保持不设。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from _ext_world import H


def row(work_id, *, retrieval="semantic", degraded=False):
    r = {"work_id": work_id, "title": f"paper {work_id}", "year": 2021, "journal": "PRL",
         "doi": f"10.1/{work_id}", "cited": 7, "score": 0.81, "source": "openalex",
         "abstract_excerpt": "Kondo resonance on Au(111)", "retrieval": retrieval}
    if degraded:
        r["degraded"] = True
    return r


@pytest.fixture()
def lit(tmp_path, monkeypatch):
    import mast.vista.service as vista_service
    from mast.api.ext import create_ext_app
    from mast.api.ext.jobs import JobManager
    from mast.api.ext.literature import router
    from mast.knowledge import literature_index

    # the ext app binds a VISTA service; this route never uses it, so a do-nothing double
    # keeps the process singleton (and its frame watcher) out of these tests
    idle = SimpleNamespace(shutdown=lambda: None)
    idle.bind = lambda ctx, **kw: idle
    monkeypatch.setattr(vista_service, "get_service", lambda ctx=None: idle)
    calls: list[dict] = []
    state = SimpleNamespace(rows=[row("W1"), row("W2")], error=None)

    def fake_search(query, k=10, material=None, **kw):
        calls.append({"query": query, "k": k, "material": material})
        if state.error:
            raise state.error
        return [dict(r) for r in state.rows][:k]

    monkeypatch.setattr(literature_index, "search", fake_search)
    ctx = SimpleNamespace(literature_wired=True, library_registry=None)
    jm = JobManager(tmp_path / "journal")
    app = create_ext_app(ctx, job_manager=jm)
    # create_ext_app mounts it already (FastAPI ≥ 0.140 keeps it as one _IncludedRouter)
    if not any(getattr(r, "original_router", None) is router
               or getattr(r, "path", None) == "/literature/search" for r in app.routes):
        app.include_router(router)
    yield SimpleNamespace(client=TestClient(app), calls=calls, state=state, ctx=ctx)
    jm.shutdown(timeout_s=1)


def search(w, **body):
    return w.client.post("/literature/search", json=body, headers=H())


def test_hits_pass_through(lit):
    r = search(lit, query="Kondo Au(111)", k=2, material="Au(111)")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2 and body["degraded"] is False and "note" not in body
    assert body["hits"][0] == {k: v for k, v in row("W1").items()}
    assert lit.calls == [{"query": "Kondo Au(111)", "k": 2, "material": "Au(111)"}]


def test_default_k_and_no_material(lit):
    search(lit, query="moire")
    assert lit.calls == [{"query": "moire", "k": 8, "material": None}]


def test_keyword_fallback_is_degraded_with_a_note(lit):
    lit.state.rows = [row("W1", retrieval="keyword", degraded=True)]
    body = search(lit, query="Kondo").json()
    assert body["degraded"] is True and body["count"] == 1 and "关键词" in body["note"]


def test_a_backend_failure_degrades_instead_of_500(lit):
    lit.state.error = RuntimeError("index missing")
    r = search(lit, query="Kondo")
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is True and body["hits"] == [] and "检索失败" in body["note"]


def test_unwired_is_degraded_and_never_searches(lit):
    lit.ctx.literature_wired = False
    body = search(lit, query="Kondo").json()
    assert body["degraded"] is True and body["hits"] == [] and "没有接线" in body["note"]
    assert lit.calls == []


def test_library_filter_over_fetches_and_keeps_members(lit, monkeypatch):
    from mast.knowledge import libraries

    monkeypatch.setattr(libraries, "get_library",
                        lambda library_id, registry=None: {"members": [{"work_id": "W2"}]})
    body = search(lit, query="Kondo", k=1, library_id="lib-exp").json()
    assert [h["work_id"] for h in body["hits"]] == ["W2"]
    assert lit.calls[0]["k"] > 1


@pytest.mark.parametrize("body", [{"query": "x", "k": 51}, {"query": "x", "k": 0}, {"query": ""},
                                  {"k": 3}])
def test_bad_requests_are_422(lit, body):
    r = lit.client.post("/literature/search", json=body, headers=H())
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
