"""``/api/optical-layouts``: what the web editor sends, and what a forged request gets.

The write endpoints are the only way a drawing of the bench (and the people's words about it) enters
MAST, so they are tested from both sides: every legitimate shape works and says what happened, and
each way a *web page* could press them without the operator meaning it is refused. The repo has been
bitten by a write endpoint with an optional body that a ``no-cors`` empty POST could press, hence the
explicit cases for every write route.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast.api.access import AccessGate
from mast.api.routes import optical_layout as routes
from mast.optical_layout import store as S

from ._layouts import budget_layout, comp, dense_field, det, laser, layout, mirror

JSON = {"Content-Type": "application/json"}
PAGE = "http://127.0.0.1:7862"


def _app(*, lan: bool = False, dev_origins=(), gate: bool = True) -> FastAPI:
    app = FastAPI()
    if gate:
        app.add_middleware(AccessGate, credentials=("op", "pw") if lan else None, dev_origins=dev_origins)
    app.include_router(routes.router, prefix="/api")
    return app


@pytest.fixture
def client(layout_root):
    return TestClient(_app(gate=False), base_url=PAGE)


@pytest.fixture
def gated(layout_root):
    return TestClient(_app(), base_url=PAGE)


def doc(lid="main", **kw) -> dict:
    comps = kw.pop("comps", None) or [laser("las1", 50, 62.5), mirror("m1", 112.5, 62.5, 135), det("d", 112.5, 300, 270)]
    return layout(comps, lid=lid, title=kw.pop("title", "Main"), **kw)


def put(client, lid="main", *, base=0, by="ana", body=None, **kw):
    payload = body if body is not None else {"layout": doc(lid), "base_revision": base, "by": by}
    return client.put(f"/api/optical-layouts/{lid}", json=payload, **kw)


def seed(client, lid="main", **kw):
    r = put(client, lid, **kw)
    assert r.status_code == 200, r.text
    return r.json()


# ── reading and saving ───────────────────────────────────────────────────────────────────────


def test_an_empty_installation_lists_nothing_and_has_no_layout(client):
    assert client.get("/api/optical-layouts").json() == {"layouts": []}
    r = client.get("/api/optical-layouts/main")
    assert r.status_code == 404 and "main" in r.json()["detail"]


def test_save_then_read_gives_the_layout_its_trace_and_the_links(client):
    saved = seed(client)
    assert set(saved) == {"layout", "trace", "links_state"}
    lay = saved["layout"]
    assert (lay["id"], lay["revision"], lay["updated_by"], lay["schema"]) == ("main", 1, "ana", 1)
    assert lay["updated_at"].endswith("+00:00") and lay["components"][1]["angle_deg"] == 135.0
    (tree,) = saved["trace"]
    assert tree["source"] == "las1" and tree["wavelength_nm"] == 532.0
    assert [e["action"] for e in tree["branches"][0]["events"]] == ["reflect", "stop"]
    assert saved["links_state"] == {}
    assert client.get("/api/optical-layouts/main").json() == saved
    (row,) = client.get("/api/optical-layouts").json()["layouts"]
    assert (row["id"], row["title"], row["revision"], row["components"], row["open_comments"]) == ("main", "Main", 1, 3, 0)


def test_what_the_client_sends_as_bookkeeping_is_replaced_by_the_servers(client):
    body = {"layout": {**doc(), "revision": 77, "updated_by": "mallory", "updated_at": "1999-01-01"}, "base_revision": 0, "by": "ana"}
    got = put(client, body=body).json()["layout"]
    assert (got["revision"], got["updated_by"]) == (1, "ana") and got["updated_at"] != "1999-01-01"


def test_the_saved_layout_is_the_normalised_one(client):
    lay = doc(comps=[laser("las1", 50.004, 62.5, -0.001), mirror("m1", 112.5, 62.5, 495)])
    got = put(client, body={"layout": lay, "base_revision": 0, "by": "ana"}).json()["layout"]
    assert got["components"][0]["x_mm"] == 50.0 and got["components"][0]["angle_deg"] == 0.0 and got["components"][1]["angle_deg"] == 135.0
    assert got["components"][1]["aperture_mm"] == 25.4, "the type's default aperture is filled in"


def test_the_path_id_must_be_the_layouts_own_id(client):
    r = client.put("/api/optical-layouts/other", json={"layout": doc("main"), "base_revision": 0, "by": "ana"})
    assert r.status_code == 422 and "main" in r.json()["detail"]
    assert client.get("/api/optical-layouts").json() == {"layouts": []}


def test_an_invalid_layout_is_a_422_that_says_where(client):
    bad = doc()
    bad["components"][0]["props"] = {"colour": "red"}
    r = put(client, body={"layout": bad, "base_revision": 0, "by": "ana"})
    assert r.status_code == 422
    assert "props" in json.dumps(r.json()["detail"], ensure_ascii=False)
    assert client.get("/api/optical-layouts").json() == {"layouts": []}


@pytest.mark.parametrize("bad_id", ["con", "a b", "x" * 41, "é", "nul"])
def test_a_bad_id_is_not_a_layout(client, bad_id):
    r = client.get(f"/api/optical-layouts/{bad_id}")
    assert r.status_code in (404, 422)
    r = client.put(f"/api/optical-layouts/{bad_id}", json={"layout": {**doc(), "id": bad_id}, "base_revision": 0, "by": "ana"})
    assert r.status_code == 422


# ── revisions: two tabs ──────────────────────────────────────────────────────────────────────


def test_a_stale_save_is_a_409_and_changes_nothing(client):
    seed(client)
    first = put(client, base=1, by="bob", body={"layout": doc(title="bob's"), "base_revision": 1, "by": "bob"})
    assert first.status_code == 200 and first.json()["layout"]["revision"] == 2
    stale = put(client, body={"layout": doc(title="ana's"), "base_revision": 1, "by": "ana"})
    assert stale.status_code == 409 and "2" in stale.json()["detail"]
    body = stale.json()
    assert set(body) == {"detail", "current_revision", "updated_by", "updated_at"}, "what the editor needs to offer 'load the latest'"
    assert (body["current_revision"], body["updated_by"]) == (2, "bob") and body["updated_at"].endswith("+00:00")
    assert client.get("/api/optical-layouts/main").json()["layout"]["title"] == "bob's"
    new_but_exists = put(client, body={"layout": doc(), "base_revision": 0, "by": "ana"})
    assert new_but_exists.status_code == 409 and new_but_exists.json()["current_revision"] == 2
    gone = put(client, "other", base=3)
    assert gone.status_code == 409 and gone.json()["current_revision"] == 0 and gone.json()["updated_by"] == "", "a layout that does not exist is revision 0"
    assert client.get("/api/optical-layouts/main").json()["layout"]["revision"] == 2


def test_two_tabs_saving_at_once_one_wins(client):
    seed(client)
    codes: list[int] = []
    threads = [threading.Thread(target=lambda n=n: codes.append(put(client, body={"layout": doc(title=f"t{n}"), "base_revision": 1, "by": f"u{n}"}).status_code))
               for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [200] + [409] * 5


def test_base_revision_is_required_and_not_negative(client):
    for body in ({"layout": doc(), "by": "ana"}, {"layout": doc(), "base_revision": -1, "by": "ana"},
                 {"layout": doc(), "base_revision": "x", "by": "ana"}, {"layout": doc(), "base_revision": 1.5, "by": "ana"}):
        assert put(client, body=body).status_code == 422, body


# ── limits ───────────────────────────────────────────────────────────────────────────────────


def test_the_documented_limits_are_enforced_through_the_api(client):
    too_many = [comp(f"c{i}", "window", i, 0, 0) for i in range(401)]
    assert put(client, body={"layout": doc(comps=too_many), "base_revision": 0, "by": "ana"}).status_code == 422
    sources = [laser(f"l{i}", i, 0) for i in range(33)]
    assert put(client, body={"layout": doc(comps=sources), "base_revision": 0, "by": "ana"}).status_code == 422
    texts = [{"id": f"t{i}", "x_mm": 0, "y_mm": 0, "text": "x"} for i in range(101)]
    assert put(client, body={"layout": {**doc(), "texts": texts}, "base_revision": 0, "by": "ana"}).status_code == 422
    fat = [mirror(f"m{i}", i, 0, 0, notes="é" * 2000, model="é" * 200, manufacturer="é" * 200, function="é" * 200,
                  actuator="é" * 200, connection="é" * 200) for i in range(200)]
    assert put(client, body={"layout": doc(comps=fat), "base_revision": 0, "by": "ana"}).status_code == 422
    assert client.get("/api/optical-layouts").json() == {"layouts": []}
    ok = [comp(f"c{i}", "window", i, 0, 0) for i in range(400)]
    assert put(client, body={"layout": doc(comps=ok), "base_revision": 0, "by": "ana"}).status_code == 200


def test_a_huge_request_body_is_refused_before_it_is_read(client):
    big = b'{"layout": {"id": "main", "notes": "' + b"x" * 3_000_000 + b'"}, "base_revision": 0, "by": "ana"}'
    r = client.put("/api/optical-layouts/main", content=big, headers=JSON)
    assert r.status_code == 413
    assert client.get("/api/optical-layouts").json() == {"layouts": []}


# ── history ──────────────────────────────────────────────────────────────────────────────────


def test_history_lists_revisions_newest_first_and_each_can_be_read_with_its_trace(client):
    seed(client)
    for i, who in enumerate(("bob", "carl"), 1):
        put(client, body={"layout": doc(title=f"v{i + 1}"), "base_revision": i, "by": who})
    rows = client.get("/api/optical-layouts/main/history").json()["revisions"]
    assert [(r["revision"], r["updated_by"], r["title"], r["components"]) for r in rows] == [(3, "carl", "v3", 3), (2, "bob", "v2", 3), (1, "ana", "Main", 3)]
    assert [r["revision"] for r in client.get("/api/optical-layouts/main/history", params={"limit": 2}).json()["revisions"]] == [3, 2]
    old = client.get("/api/optical-layouts/main/revisions/2").json()
    assert old["revision"] == 2 and old["updated_by"] == "bob" and old["layout"]["title"] == "v2" and old["trace"]
    assert client.get("/api/optical-layouts/main/revisions/99").status_code == 404
    assert client.get("/api/optical-layouts/nope/history").status_code == 404
    for limit in (0, 201, "x"):
        assert client.get("/api/optical-layouts/main/history", params={"limit": limit}).status_code == 422


def test_only_the_newest_200_revisions_are_kept(client):
    seed(client)
    for i in range(1, 204):
        assert put(client, body={"layout": doc(title=f"v{i + 1}"), "base_revision": i, "by": "ana"}).status_code == 200
    rows = client.get("/api/optical-layouts/main/history", params={"limit": 200}).json()["revisions"]
    assert len(rows) == 200 and rows[0]["revision"] == 204 and rows[-1]["revision"] == 5
    assert client.get("/api/optical-layouts/main/revisions/5").status_code == 200
    assert client.get("/api/optical-layouts/main/revisions/4").status_code == 404


def test_going_back_to_a_version_is_saving_its_content_as_a_new_one(client):
    seed(client)
    put(client, body={"layout": doc(title="changed"), "base_revision": 1, "by": "bob"})
    old = client.get("/api/optical-layouts/main/revisions/1").json()["layout"]
    back = put(client, body={"layout": old, "base_revision": 2, "by": "ana"}).json()["layout"]
    assert back["revision"] == 3 and back["title"] == "Main" and back["updated_by"] == "ana"


# ── archive ──────────────────────────────────────────────────────────────────────────────────


def test_delete_archives_and_a_stale_or_unknown_one_is_refused(client, layout_root):
    seed(client)
    stale = client.post("/api/optical-layouts/main/delete", json={"by": "bob", "base_revision": 0})
    assert stale.status_code == 409 and stale.json()["current_revision"] == 1 and stale.json()["updated_by"] == "ana"
    assert client.post("/api/optical-layouts/nope/delete", json={"by": "bob", "base_revision": 1}).status_code == 404
    r = client.post("/api/optical-layouts/main/delete", json={"by": "bob", "base_revision": 1})
    assert r.status_code == 200 and r.json() == {"id": "main", "archived": True}
    assert client.get("/api/optical-layouts/main").status_code == 404 and client.get("/api/optical-layouts").json() == {"layouts": []}
    archive = layout_root / "config" / "optical_layouts" / "archive"
    assert any(p.name.startswith("main-") and p.name.endswith(".json") for p in archive.iterdir()), "archived, not deleted"
    assert put(client).status_code == 200, "the id is free again"


# ── comments ─────────────────────────────────────────────────────────────────────────────────


def test_the_operator_comments_and_resolves(client):
    seed(client)
    r = client.post("/api/optical-layouts/main/comments", json={"by": "ana", "text": "Why 135?", "component_id": "m1"})
    assert r.status_code == 200
    c = r.json()
    assert c["id"] == "c1" and c["author"] == "operator" and c["by"] == "ana" and c["component_id"] == "m1" and c["resolved"] is False
    assert "client" not in c
    listing = client.get("/api/optical-layouts/main/comments").json()
    assert listing["open_count"] == 1 and [x["id"] for x in listing["comments"]] == ["c1"]
    assert client.get("/api/optical-layouts").json()["layouts"][0]["open_comments"] == 1
    done = client.post("/api/optical-layouts/main/comments/c1/resolve", json={"by": "bob"}).json()
    assert done["resolved"] is True and done["resolved_by"] == "bob"
    again = client.post("/api/optical-layouts/main/comments/c1/resolve", json={"by": "carl"}).json()
    assert again["resolved_by"] == "bob"
    after = client.get("/api/optical-layouts/main/comments").json()
    assert after["open_count"] == 0 and len(after["comments"]) == 1
    assert client.get("/api/optical-layouts/main/comments", params={"status": "open"}).json()["comments"] == []
    assert client.get("/api/optical-layouts/main/comments", params={"status": "bogus"}).status_code == 422


def test_comment_errors(client):
    seed(client)
    url = "/api/optical-layouts/main/comments"
    assert client.post(url, json={"by": "ana", "text": "x", "component_id": "ghost"}).status_code == 422
    assert client.post(url, json={"by": "ana", "text": ""}).status_code == 422
    assert client.post(url, json={"by": "ana", "text": "   "}).status_code == 422
    assert client.post(url, json={"by": "ana", "text": "x" * 2001}).status_code == 422
    assert client.post(url, json={"by": "ana", "text": "x", "component_id": "bad id"}).status_code == 422
    assert client.post("/api/optical-layouts/nope/comments", json={"by": "ana", "text": "x"}).status_code == 404
    assert client.post("/api/optical-layouts/main/comments/c9/resolve", json={"by": "ana"}).status_code == 404
    assert client.get("/api/optical-layouts/nope/comments").status_code == 404


def test_a_full_layout_refuses_more_comments_with_429(client, layout_root):
    seed(client)
    path = layout_root / "config" / "optical_layouts" / "main.comments.json"
    rows = [{"id": f"c{i}", "author": "agent", "by": "ext:x", "text": "t", "component_id": None,
             "created_at": "2026-01-01T00:00:00+00:00", "resolved": False} for i in range(1, 1001)]
    path.write_text(json.dumps({"version": 1, "next": 1001, "comments": rows}), encoding="utf-8")
    r = client.post("/api/optical-layouts/main/comments", json={"by": "ana", "text": "one more"})
    assert r.status_code == 429


# ── a file that cannot be read, a disk that refuses ──────────────────────────────────────────


def test_a_broken_file_is_a_500_that_names_it_and_is_never_overwritten(client, layout_root, caplog):
    path = layout_root / "config" / "optical_layouts" / "main.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{definitely not json")
    with caplog.at_level(logging.WARNING):
        r = client.get("/api/optical-layouts/main")
    assert r.status_code == 500 and "main.json" in r.json()["detail"]
    assert put(client).status_code == 500
    assert client.post("/api/optical-layouts/main/delete", json={"by": "ana", "base_revision": 1}).status_code == 500
    assert path.read_bytes() == b"{definitely not json"
    (row,) = client.get("/api/optical-layouts").json()["layouts"]
    assert row["id"] == "main" and row["error"]
    assert any("main.json" in rec.getMessage() for rec in caplog.records)


def test_a_disk_that_refuses_is_a_503_and_nothing_changed(client, monkeypatch):
    seed(client)

    def boom(path, data):
        raise PermissionError("locked")

    with monkeypatch.context() as m:
        m.setattr(S, "atomic_write", boom)
        r = put(client, body={"layout": doc(title="lost"), "base_revision": 1, "by": "ana"})
    assert r.status_code == 503
    assert "什么都没有改变" in r.json()["detail"] or "Nothing was changed" in r.json()["detail"]
    got = client.get("/api/optical-layouts/main").json()["layout"]
    assert got["revision"] == 1 and got["title"] == "Main"


# ── links ────────────────────────────────────────────────────────────────────────────────────


def test_links_state_is_attached_for_components_that_have_a_link(client, monkeypatch):
    from mast.operator_hitl import optical_state

    class Reg:
        def list_devices(self):
            return [{"id": "hwp_rot", "name": "Rotator", "type": "pztc", "kind": "motion", "enabled": True, "connected": False}]

        def built_station_devices(self):
            return {}

    import mast.instruments.registry as registry_module

    old = optical_state.set_store(optical_state.OpticalStateStore())
    try:
        optical_state.get_store().set_by_operator("excitation_waveplate", "hwp_532", by="ana", source="test")
        with monkeypatch.context() as m:
            m.setattr(registry_module, "get_instrument_registry", lambda: Reg())
            hwp = comp("hwp1", "waveplate", 150, 62.5, 0)
            hwp["links"] = {"device_id": "hwp_rot", "slot": "excitation_waveplate"}
            got = seed(client, body={"layout": doc(comps=[laser("las1", 50, 62.5), hwp]), "base_revision": 0, "by": "ana"})
    finally:
        optical_state.set_store(old)
    assert set(got["links_state"]) == {"hwp1"}
    assert got["links_state"]["hwp1"]["device"]["known"] is True and got["links_state"]["hwp1"]["slot"]["value"] == "hwp_532"


# ── render ───────────────────────────────────────────────────────────────────────────────────


def test_render_png_is_a_png_with_the_asked_width(client):
    seed(client)
    r = client.get("/api/optical-layouts/main/render.png", params={"width": 640})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.headers["cache-control"] == "no-store"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n" and int.from_bytes(r.content[16:20], "big") == 640
    assert client.get("/api/optical-layouts/main/render.png").status_code == 200
    zoom = client.get("/api/optical-layouts/main/render.png", params={"width": 640, "fit": "content"})
    assert zoom.status_code == 200 and zoom.content != r.content
    for params in ({"width": 199}, {"width": 3001}, {"width": "x"}, {"fit": "everything"}):
        assert client.get("/api/optical-layouts/main/render.png", params=params).status_code == 422
    assert client.get("/api/optical-layouts/nope/render.png").status_code == 404


# ── the body is mandatory and strict ─────────────────────────────────────────────────────────

WRITES = [
    ("put", "/api/optical-layouts/main", lambda: {"layout": doc(), "base_revision": 0, "by": "ana"}),
    ("post", "/api/optical-layouts/main/delete", lambda: {"by": "ana", "base_revision": 1}),
    ("post", "/api/optical-layouts/main/comments", lambda: {"by": "ana", "text": "hi"}),
    ("post", "/api/optical-layouts/main/comments/c1/resolve", lambda: {"by": "ana"}),
]


def _state(client) -> tuple:
    r = client.get("/api/optical-layouts/main")
    comments = client.get("/api/optical-layouts/main/comments")
    return (r.status_code, r.json().get("layout", {}).get("revision"), comments.json())


def _armed(client):
    """A layout with one open comment, so every write route has something real to act on."""
    seed(client)
    client.post("/api/optical-layouts/main/comments", json={"by": "ana", "text": "first"})
    return _state(client)


@pytest.mark.parametrize("method, path, good", WRITES, ids=[w[1].rsplit("/", 1)[-1] for w in WRITES])
def test_an_empty_post_does_nothing_even_with_a_json_header(client, method, path, good):
    before = _armed(client)
    for kw in (dict(headers=JSON), dict(headers=JSON, content=b""), dict(headers=JSON, content=b"null"),
               dict(headers=JSON, content=b"{}"), dict(headers=JSON, content=b"[]"), dict(headers=JSON, content=b'"x"')):
        r = getattr(client, method)(path, **kw)
        assert r.status_code == 422, (kw, r.status_code, r.text)
    assert _state(client) == before


@pytest.mark.parametrize("method, path, good", WRITES, ids=[w[1].rsplit("/", 1)[-1] for w in WRITES])
def test_a_body_without_a_json_content_type_is_refused_before_anything(client, method, path, good):
    """What a ``no-cors`` page can send: text/plain, a form, or no Content-Type at all."""
    before = _armed(client)
    raw = json.dumps(good()).encode()
    for headers in ({"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"},
                    {"Content-Type": "multipart/form-data; boundary=x"}, {}):
        r = getattr(client, method)(path, content=raw, headers=headers)
        assert r.status_code == 415, (headers, r.status_code, r.text)
    assert _state(client) == before


@pytest.mark.parametrize("method, path, good", WRITES, ids=[w[1].rsplit("/", 1)[-1] for w in WRITES])
def test_unknown_fields_and_a_missing_or_blank_name_are_refused(client, method, path, good):
    before = _armed(client)
    body = good()
    for bad in ({**body, "approved": True}, {**body, "client": {"peer": "forged"}}, {**body, "by": ""}, {**body, "by": "   "},
                {k: v for k, v in body.items() if k != "by"}, {**body, "by": "x" * 81}, {**body, "source": "web"}):
        assert getattr(client, method)(path, json=bad).status_code == 422, bad
    assert _state(client) == before


def test_get_endpoints_need_no_body_and_there_is_no_other_way_to_write(client):
    seed(client)
    for path in ("/api/optical-layouts", "/api/optical-layouts/main", "/api/optical-layouts/main/history",
                 "/api/optical-layouts/main/comments", "/api/optical-layouts/main/render.png", "/api/optical-layouts/main/revisions/1"):
        assert client.get(path).status_code == 200
    assert client.post("/api/optical-layouts/main").status_code in (404, 405)
    assert client.delete("/api/optical-layouts/main").status_code == 405
    assert client.patch("/api/optical-layouts/main", json={}).status_code == 405
    assert client.put("/api/optical-layouts/main/comments", json={"by": "a", "text": "x"}).status_code == 405


# ── origin / host checks ─────────────────────────────────────────────────────────────────────


def _browser(client, *, origin=PAGE, site="same-origin", host=None, auth=None, lid="main", base=0):
    headers = {"Origin": origin, "Sec-Fetch-Site": site, "Sec-Fetch-Mode": "cors", **JSON}
    if host:
        headers["Host"] = host
    if auth:
        headers["Authorization"] = auth
    return client.put(f"/api/optical-layouts/{lid}", json={"layout": doc(lid), "base_revision": base, "by": "ana"}, headers=headers)


def test_the_operators_own_page_can_write(client):
    assert _browser(client).status_code == 200
    assert _browser(client, site="none", base=1).status_code == 200, "a typed-in / bookmarked navigation"


@pytest.mark.parametrize("kw", [
    dict(origin="http://evil.example"),
    dict(origin="http://127.0.0.1:9999"),                                         # another local page
    dict(origin="null"),
    dict(site="cross-site"),
    dict(site="same-site"),
    dict(host="evil.example:7862", origin="http://evil.example:7862"),             # DNS rebinding
], ids=["evil", "other-port", "null", "cross-site", "same-site", "rebinding"])
def test_a_page_from_elsewhere_cannot_write_any_route(client, kw):
    assert _browser(client, **kw).status_code == 403
    seed(client)
    headers = {"Origin": kw.get("origin", PAGE), "Sec-Fetch-Site": kw.get("site", "same-origin"), **JSON}
    if kw.get("host"):
        headers["Host"] = kw["host"]
    for method, path, good in WRITES[1:]:
        r = getattr(client, method)(path, json=good(), headers=headers)
        assert r.status_code == 403, (path, r.status_code)
    assert client.get("/api/optical-layouts/main").json()["layout"]["revision"] == 1
    assert client.get("/api/optical-layouts/main/comments").json()["comments"] == []


def test_the_origin_check_does_not_depend_on_the_global_gate(client):
    assert _browser(client, origin="http://evil.example").status_code == 403


def test_behind_the_real_gate_a_foreign_origin_is_refused_too(gated):
    assert _browser(gated, origin="http://evil.example").status_code == 403
    assert _browser(gated).status_code == 200


def test_a_host_name_is_accepted_only_with_lan_credentials(layout_root):
    host = "rig.tail1234.ts.net:7862"
    page = f"https://{host}"
    loop = TestClient(_app(), base_url=PAGE)
    assert _browser(loop, origin=page, host=host).status_code == 403
    basic = "Basic " + base64.b64encode(b"op:pw").decode()
    lan = TestClient(_app(lan=True), base_url=page)
    assert _browser(lan, origin=page, host=host, auth=basic).status_code == 200
    assert _browser(TestClient(_app(lan=True), base_url=page), origin=page, host=host).status_code == 401
    assert _browser(TestClient(_app(lan=True), base_url=page), origin="https://evil.example", host=host, auth=basic, lid="other").status_code == 403


def test_dev_origins_of_the_gate_are_honoured(layout_root):
    c = TestClient(_app(dev_origins=("http://localhost:*",)), base_url=PAGE)
    assert _browser(c, origin="http://localhost:5173").status_code == 200
    assert _browser(TestClient(_app(), base_url=PAGE), origin="http://localhost:5173", lid="other").status_code == 403


def test_programmatic_clients_need_no_browser_headers(client):
    assert put(client).status_code == 200


# ── provenance ───────────────────────────────────────────────────────────────────────────────


def test_every_write_records_what_the_server_saw_of_the_request(client, layout_root):
    headers = {"Origin": PAGE, "Sec-Fetch-Site": "same-origin", "User-Agent": "pytest-ui", **JSON}
    client.put("/api/optical-layouts/main", json={"layout": doc(), "base_revision": 0, "by": "ana", }, headers=headers)
    client.post("/api/optical-layouts/main/comments", json={"by": "ana", "text": "hi"}, headers=headers)
    hist = (layout_root / "config" / "optical_layouts" / "history" / "main.jsonl").read_text(encoding="utf-8").splitlines()
    seen = json.loads(hist[0])["client"]
    assert seen["browser"] is True and seen["origin"] == PAGE and seen["user_agent"] == "pytest-ui" and seen["sec_fetch_site"] == "same-origin"
    stored = json.loads((layout_root / "config" / "optical_layouts" / "main.comments.json").read_text(encoding="utf-8"))
    assert stored["comments"][0]["client"]["user_agent"] == "pytest-ui", "kept for the audit ..."
    assert "client" not in client.get("/api/optical-layouts/main/comments").json()["comments"][0], "... not handed out"


def test_every_write_leaves_one_log_line_with_the_outcome(client, caplog):
    with caplog.at_level(logging.INFO, logger="mast.api.routes.optical_layout"):
        put(client)
        put(client)                                                    # 409
        client.post("/api/optical-layouts/main/comments", json={"by": "ana", "text": "hi"})
    lines = [r.getMessage() for r in caplog.records if "optical layout write" in r.getMessage()]
    assert any("action=save" in ln and 'by="ana"' in ln and "outcome=ok" in ln for ln in lines)
    assert any("action=save" in ln and "outcome=refused_409" in ln for ln in lines)
    assert any("action=comment" in ln and "outcome=ok" in ln for ln in lines)
    assert all("client=" in ln for ln in lines)


# ── mounted in the real app ──────────────────────────────────────────────────────────────────


def test_the_routes_are_part_of_the_main_app_and_not_of_the_ext_surface():
    from mast.api.app import create_app

    spec = create_app(dev_cors=False).openapi()
    mine = {(m.upper(), p) for p, ops in spec["paths"].items() if "optical-layouts" in p for m in ops}
    assert mine == {("GET", "/api/optical-layouts"), ("GET", "/api/optical-layouts/{layout_id}"), ("PUT", "/api/optical-layouts/{layout_id}"),
                    ("POST", "/api/optical-layouts/{layout_id}/delete"), ("GET", "/api/optical-layouts/{layout_id}/history"),
                    ("GET", "/api/optical-layouts/{layout_id}/revisions/{revision}"), ("GET", "/api/optical-layouts/{layout_id}/render.png"),
                    ("GET", "/api/optical-layouts/{layout_id}/comments"), ("POST", "/api/optical-layouts/{layout_id}/comments"),
                    ("POST", "/api/optical-layouts/{layout_id}/comments/{comment_id}/resolve")}
    assert not [p for p in spec["paths"] if p.startswith("/api/ext")]
    assert {"Layout", "LayoutWriteBody", "LayoutViewResponse", "CommentOut"} <= set(spec["components"]["schemas"])


def test_a_write_through_the_real_app_is_gated_like_every_other_write(layout_root):
    from mast.api.app import create_app

    c = TestClient(create_app(dev_cors=False), base_url=PAGE)
    body = {"layout": doc(), "base_revision": 0, "by": "ana"}
    assert c.put("/api/optical-layouts/main", content=json.dumps(body).encode(), headers={"Content-Type": "text/plain"}).status_code == 415
    assert c.put("/api/optical-layouts/main", json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.get("/api/optical-layouts").json() == {"layouts": []}
    assert c.put("/api/optical-layouts/main", json=body).status_code == 200
    assert c.get("/api/optical-layouts/main").json()["layout"]["revision"] == 1
    assert c.get("/api/optical-layouts/main/render.png", params={"width": 300}).headers["content-type"] == "image/png"


def test_a_urllib_client_works_against_a_real_server(layout_root):
    """Scripts and the launcher use urllib (no browser headers): over a real socket, behind the real app."""
    import urllib.error
    import urllib.request

    import uvicorn

    from mast.api.app import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(dev_cors=False), host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/api/optical-layouts"

    def call(method, path="", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={**({"Content-Type": "application/json"} if data else {}), **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    try:
        status, raw = call("PUT", "/main", {"layout": doc(), "base_revision": 0, "by": "ana"})
        assert status == 200 and json.loads(raw)["layout"]["revision"] == 1
        status, raw = call("GET", "/main/render.png?width=300")
        assert status == 200 and raw[:8] == b"\x89PNG\r\n\x1a\n"
        req = urllib.request.Request(base + "/main/comments", data=b"", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=20)
        assert exc.value.code == 415
        assert call("POST", "/main/comments", {"by": "ana", "text": "hi"})[0] == 200
    finally:
        server.should_exit = True
        t.join(timeout=20)


# ── a trace the budget cut ───────────────────────────────────────────────────────────────────


def test_a_cut_trace_passes_the_response_model_on_every_route_that_returns_one(client, monkeypatch):
    """``end`` is an enum in the response model: a value the tracer makes and the model does not know is
    a 500 on the page that shows the layout."""
    from mast.optical_layout import trace as T

    lay = {**budget_layout(), "id": "bud", "title": "Budget"}
    monkeypatch.setattr(T, "MAX_TRACE_EVENTS", 3)
    saved = put(client, "bud", body={"layout": lay, "base_revision": 0, "by": "ana"})
    assert saved.status_code == 200, saved.text
    for r in (saved, client.get("/api/optical-layouts/bud"), client.get("/api/optical-layouts/bud/revisions/1")):
        assert r.status_code == 200, r.text
        trace = r.json()["trace"]
        assert sum(len(b["events"]) for t in trace for b in t["branches"]) == 3
        assert trace[-1]["branches"][-1]["end"] == "budget" and [t["source"] for t in trace] == ["las1"]
    assert client.get("/api/optical-layouts/bud/render.png", params={"width": 400}).status_code == 200


def test_the_audits_worst_case_goes_through_the_routes_cut_and_in_seconds(client):
    """A worst case: 393 components, 60 KB, within every limit. It took a 17 s trace and a 61 MB answer
    on every read, in a thread-pool worker."""
    lay = {**dense_field(), "id": "worst", "title": "Worst case"}
    started = time.perf_counter()
    assert put(client, "worst", body={"layout": lay, "base_revision": 0, "by": "ana"}).status_code == 200
    got = client.get("/api/optical-layouts/worst")
    assert got.status_code == 200
    trace = got.json()["trace"]
    assert sum(len(b["events"]) for t in trace for b in t["branches"]) == 5000
    assert trace[-1]["branches"][-1]["end"] == "budget"
    assert len(got.content) < 4_000_000
    assert client.get("/api/optical-layouts/worst/render.png", params={"width": 800}).status_code == 200
    assert time.perf_counter() - started < 40


# ── the length of a write body is declared (411) and bounded (413), before it is read ────────


def _chunks(*parts: bytes):
    seen: list[int] = []

    def gen():
        for part in parts:
            seen.append(len(part))
            yield part

    return gen(), seen


@pytest.mark.parametrize("method, path, good", WRITES, ids=[w[1].rsplit("/", 1)[-1] for w in WRITES])
def test_a_chunked_write_is_a_411_and_its_body_is_never_read(client, method, path, good):
    before = _armed(client)
    raw = json.dumps(good()).encode()
    body, seen = _chunks(raw[:20], raw[20:])
    r = getattr(client, method)(path, content=body, headers=JSON)
    assert r.status_code == 411, (r.status_code, r.text)
    assert "Content-Length" in r.json()["detail"]
    assert seen == [], "the server asked for the body of a request it was going to refuse"
    assert _state(client) == before


@pytest.mark.parametrize("method, path, good", WRITES, ids=[w[1].rsplit("/", 1)[-1] for w in WRITES])
def test_a_body_with_a_transfer_encoding_is_refused_even_when_it_also_claims_a_small_length(client, method, path, good):
    """Chunked framing wins over Content-Length: the small number would bound nothing."""
    before = _armed(client)
    raw = json.dumps(good()).encode()
    r = getattr(client, method)(path, content=raw, headers={**JSON, "Transfer-Encoding": "chunked"})
    assert r.status_code == 411
    assert _state(client) == before


@pytest.mark.parametrize("method, path, good", WRITES, ids=[w[1].rsplit("/", 1)[-1] for w in WRITES])
def test_too_long_stays_a_413_and_a_length_that_is_not_a_number_is_a_400(client, method, path, good):
    before = _armed(client)
    raw = json.dumps(good()).encode()
    for declared, status in (("2000001", 413), ("999999999999", 413), ("9" * 5000, 413), ("-1", 400), ("1e3", 400),
                             ("abc", 400), ("", 400)):
        r = getattr(client, method)(path, content=raw, headers={**JSON, "Content-Length": declared})
        assert r.status_code == status, (declared[:20], r.status_code, r.text)
    assert _state(client) == before


def test_the_length_rule_judges_the_headers_alone():
    from mast.api.body_limit import length_refusal

    def judge(**headers):
        got = length_refusal({k.replace("_", "-"): v for k, v in headers.items()}, 2_000_000)
        return None if got is None else got[:2]

    assert judge(content_length="0") is None and judge(content_length="12") is None and judge(content_length=" 12 ") is None
    assert judge(content_length="2000000") is None
    assert judge(content_length="2000001") == (413, "request_too_large")
    assert judge(content_length="9" * 5000) == (413, "request_too_large")          # (int() of that raises)
    assert judge() == (411, "length_required")
    assert judge(transfer_encoding="chunked") == (411, "length_required")
    assert judge(transfer_encoding="chunked", content_length="5") == (411, "length_required")
    assert judge(transfer_encoding="gzip, chunked", content_length="5") == (411, "length_required")
    for bad in ("-1", "1.5", "0x10", "abc", "", " ", "1 2", "١٢", "²", "+5"):
        assert judge(content_length=bad) == (400, "bad_content_length"), repr(bad)


def test_reads_need_no_length(client):
    seed(client)
    assert client.get("/api/optical-layouts/main").status_code == 200
    assert client.get("/api/optical-layouts/main/render.png").status_code == 200


def _serve(app):
    """Run *app* on a real socket (a throw-away server in this process) -> (server, thread, port)."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    return server, t, server.servers[0].sockets[0].getsockname()[1]


def _ask(port: int, head: list[str], body: bytes = b"", *, wait: float = 6.0) -> tuple[int, bytes]:
    """Send the request line and headers (and *body*) and read the answer: (status, raw answer)."""
    import socket

    with socket.create_connection(("127.0.0.1", port), timeout=wait) as s:
        s.sendall(("\r\n".join(head) + "\r\n\r\n").encode("ascii") + body)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return int(data.split(b" ", 2)[1]), data


@pytest.mark.parametrize("method, path, good", WRITES, ids=[w[1].rsplit("/", 1)[-1] for w in WRITES])
def test_over_a_real_socket_the_answer_comes_before_any_body_is_sent(layout_root, method, path, good):
    """The client sends the headers and then nothing: a server that waits for the body (FastAPI reads it
    before it looks at a dependency) never answers and the read times out; this one refuses at once."""
    server, t, port = _serve(_app(gate=False))
    base = [f"{method.upper()} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Content-Type: application/json"]
    try:
        status, raw = _ask(port, base + ["Transfer-Encoding: chunked"])
        assert status == 411, raw[:200]
        status, raw = _ask(port, base + ["Content-Length: 3000000"])
        assert status == 413, raw[:200]
        status, raw = _ask(port, base + ["Transfer-Encoding: chunked", "Content-Length: 5"])
        assert status in (400, 411), raw[:200]                  # (an HTTP parser may refuse that combination first)
        status, raw = _ask(port, base + ["Content-Length: 0"])
        assert status == 422, raw[:200]                         # a declared length passes this guard; the body is mandatory
    finally:
        server.should_exit = True
        t.join(timeout=20)


# ── one trace per request ────────────────────────────────────────────────────────────────────


def test_a_request_traces_the_layout_once_and_hands_it_on(client, monkeypatch):
    """The answer, the description and the picture all want the trace; the trace is the expensive part of
    a read, so each request makes one (the picture of a request is made from the one it traced)."""
    from mast.optical_layout import render as render_mod
    from mast.optical_layout import summary as summary_mod
    from mast.optical_layout import trace as T

    seed(client)
    calls: list[str] = []
    real = T.trace_layout

    def counting(*a, **k):
        calls.append("trace")
        return real(*a, **k)

    with monkeypatch.context() as m:
        for mod in (routes, render_mod, summary_mod):
            m.setattr(mod, "trace_layout", counting)
        for path in ("/api/optical-layouts/main", "/api/optical-layouts/main/revisions/1",
                     "/api/optical-layouts/main/render.png", "/api/optical-layouts/main/render.png?fit=content"):
            calls.clear()
            assert client.get(path).status_code == 200, path
            assert calls == ["trace"], (path, calls)
        calls.clear()
        assert put(client, base=1).status_code == 200                # the answer to a save carries the trace too
        assert calls == ["trace"]
