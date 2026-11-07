"""外部面的光路图：只读 + 评论。

agent 能读到用户画的图（图本身、追迹、文字叙述、链接状态、未解决评论）和一张 PNG，能发评论；
**不能改几何和注释，不能解决评论** —— 这件事在这里和 ``tests/v2/unit/optical_layout/
test_layout_ext_readonly.py`` 里钉死。简报多一段 ``optical_layouts``，没有图时为空。
"""
from __future__ import annotations

import json

import pytest

from _ext_world import H

from mast.optical_layout import store as S  # noqa: E402
from mast.optical_layout.model import Layout  # noqa: E402


def comp(cid, typ, x, y, angle=0.0, *, label=None, props=None, **annotations):
    c = {"id": cid, "type": typ, "label": cid.upper() if label is None else label, "x_mm": x, "y_mm": y,
         "angle_deg": angle, "props": props or {}}
    if annotations:
        c["annotations"] = annotations
    return c


def laser(cid, x, y, angle=0.0, **kw):
    return comp(cid, "laser", x, y, angle, props={"wavelength_nm": 532}, **kw)


def mirror(cid, x, y, angle, **kw):
    return comp(cid, "mirror", x, y, angle, **kw)


def det(cid, x, y, angle, **kw):
    return comp(cid, "detector", x, y, angle, **kw)


def layout(components, *, lid="t", title=""):
    return {"schema": 1, "id": lid, "title": title, "components": components}


def _save(lid="shg", title="SHG", by="ana", comps=None, **kw):
    comps = comps or [laser("las1", 50, 62.5, label="L1", manufacturer="Cobolt", model="Samba"),
                      mirror("m1", 112.5, 62.5, 135, label="M1"), det("d", 112.5, 300, 270, label="D1")]
    return S.get_store().save(Layout.model_validate(layout(comps, lid=lid, title=title, **kw)), base_revision=0, by=by)


@pytest.fixture
def ow(world, monkeypatch):
    monkeypatch.setenv("MAST_LANG", "zh")          # the text the server writes is in the process language here
    return world


def test_an_installation_without_layouts_lists_none_and_the_briefing_says_nothing_about_them(ow):
    assert ow.client.get("/optical-layouts", headers=H()).json() == {"layouts": []}
    b = ow.client.get("/briefing", headers=H()).json()
    sec = b["sections"]["optical_layouts"]
    assert sec["ok"] is True and sec["text"] == "" and sec["data"] == {"count": 0, "layouts": []}
    assert "optical_layouts" not in b["text"], "an empty section takes no room in the digest"
    assert not [d for d in b["degraded"] if d["section"] == "optical_layouts"]


def test_a_layout_is_read_with_its_trace_its_description_and_its_comments(ow):
    _save()
    S.get_store().add_comment("shg", text="Is M1 25.4 mm?", by="ana", author="operator", component_id="m1",
                              client={"peer": "192.0.2.23", "user_agent": "ui"})
    r = ow.client.get("/optical-layouts/shg", headers=H())
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"layout", "trace", "summary", "links_state", "comments"}
    assert body["layout"]["id"] == "shg" and body["layout"]["components"][1]["angle_deg"] == 135.0
    assert body["trace"][0]["branches"][0]["events"][0]["action"] == "reflect"
    assert "L1" in body["summary"] and "M1" in body["summary"]
    assert "c1" in body["summary"] and "Is M1 25.4 mm?" in body["summary"], "open comments are in the text"
    assert [c["id"] for c in body["comments"]] == ["c1"] and body["comments"][0]["author"] == "operator"
    assert "192.0.2.23" not in json.dumps(body), "what the server saw of the operator's browser is not handed to an agent"
    (row,) = ow.client.get("/optical-layouts", headers=H()).json()["layouts"]
    assert (row["id"], row["components"], row["open_comments"]) == ("shg", 3, 1)


def test_unknown_layouts_and_bad_ids_are_json_404s(ow):
    for path in ("/optical-layouts/nope", "/optical-layouts/nope/render.png", "/optical-layouts/con", "/optical-layouts/a%20b"):
        r = ow.client.get(path, headers=H())
        assert r.status_code == 404 and r.json()["error"] == "unknown_layout", (path, r.status_code, r.text)
    r = ow.client.post("/optical-layouts/nope/comments", json={"text": "x"}, headers=H())
    assert r.status_code == 404 and r.json()["error"] == "unknown_layout"


def test_a_layout_file_that_cannot_be_read_is_a_500_that_says_so_and_is_left_alone(ow, layout_root_of):
    p = layout_root_of / "shg.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"{nope")
    r = ow.client.get("/optical-layouts/shg", headers=H())
    assert r.status_code == 500 and r.json()["error"] == "layout_unreadable" and "shg.json" in r.json()["detail"]
    assert p.read_bytes() == b"{nope"
    assert ow.client.get("/optical-layouts", headers=H()).json()["layouts"][0]["error"]
    sec = ow.client.get("/briefing", headers=H()).json()["sections"]["optical_layouts"]
    assert sec["ok"] and "shg" in sec["text"] and "文件读不了" in sec["text"]


@pytest.fixture
def layout_root_of(tmp_path):
    return tmp_path / "proj" / "config" / "optical_layouts"


def test_the_picture_is_a_png(ow):
    _save()
    r = ow.client.get("/optical-layouts/shg/render.png", headers=H(), params={"width": 500})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(r.content[16:20], "big") == 500
    whole = ow.client.get("/optical-layouts/shg/render.png", headers=H(), params={"width": 500, "fit": "table"})
    assert whole.status_code == 200 and whole.content != r.content, "the default zooms to the work"
    for params in ({"width": 199}, {"width": 3001}, {"fit": "x"}):
        assert ow.client.get("/optical-layouts/shg/render.png", headers=H(), params=params).status_code == 422


# ── comments ─────────────────────────────────────────────────────────────────────────────────


def test_an_agent_comment_is_signed_with_the_agent_id_and_is_open_for_the_operator(ow):
    _save()
    r = ow.client.post("/optical-layouts/shg/comments", json={"text": "The beam misses M2.", "component_id": "m1"}, headers=H("claude"))
    assert r.status_code == 200
    c = r.json()["comment"]
    assert r.json()["ok"] is True and c["id"] == "c1" and c["author"] == "agent" and c["by"] == "ext:claude"
    assert c["component_id"] == "m1" and c["resolved"] is False and "client" not in c
    stored = S.get_store().comments("shg")[0]
    assert stored["client"] == {"via": "ext", "actor": "claude", "session": "s1"}
    assert S.get_store().open_comment_count("shg") == 1
    # the next read of the layout shows it, in the text too
    body = ow.client.get("/optical-layouts/shg", headers=H("claude")).json()
    assert "The beam misses M2." in body["summary"] and body["comments"][0]["by"] == "ext:claude"


def test_the_author_is_the_caller_and_nothing_in_the_body_can_change_that(ow):
    _save()
    for extra in ({"by": "ana"}, {"author": "operator"}, {"resolved": True}, {"client": {"peer": "x"}}, {"layout": {}}, {"resolve": "c1"}):
        r = ow.client.post("/optical-layouts/shg/comments", json={"text": "hi", **extra}, headers=H("claude"))
        assert r.status_code == 422, (extra, r.text)
    assert S.get_store().comments("shg") == []
    ow.client.post("/optical-layouts/shg/comments", json={"text": "hi"}, headers={})
    assert S.get_store().comments("shg")[0]["by"] == "ext:anonymous"


def test_a_comment_is_checked(ow):
    _save()
    url = "/optical-layouts/shg/comments"
    r = ow.client.post(url, json={"text": "x", "component_id": "ghost"}, headers=H())
    assert r.status_code == 422 and r.json()["error"] == "invalid_comment" and "ghost" in r.json()["detail"]
    assert ow.client.post(url, json={"text": ""}, headers=H()).status_code == 422
    assert ow.client.post(url, json={"text": "   "}, headers=H()).json()["error"] == "invalid_comment"
    assert ow.client.post(url, json={"text": "x" * 2001}, headers=H()).status_code == 422
    assert ow.client.post(url, json={}, headers=H()).status_code == 422
    assert ow.client.post(url, json={"text": "x", "component_id": "bad id"}, headers=H()).status_code == 422
    assert S.get_store().comments("shg") == []


def test_an_agent_may_have_fifty_unresolved_comments_per_layout_and_the_operator_can_make_room(ow):
    _save()
    url = "/optical-layouts/shg/comments"
    for i in range(50):
        assert ow.client.post(url, json={"text": f"q{i}"}, headers=H("a")).status_code == 200
    r = ow.client.post(url, json={"text": "one too many"}, headers=H("a"))
    assert r.status_code == 429 and r.json()["error"] == "too_many_open_comments" and r.json()["limit"] == 50
    assert ow.client.post(url, json={"text": "another agent"}, headers=H("b")).status_code == 200
    S.get_store().resolve_comment("shg", "c1", by="ana")           # only a person can
    assert ow.client.post(url, json={"text": "room again"}, headers=H("a")).status_code == 200


def test_a_layout_holds_at_most_a_thousand_comments(ow, layout_root_of):
    _save()
    rows = [{"id": f"c{i}", "author": "operator", "by": "ana", "text": "t", "component_id": None,
             "created_at": "2026-01-01T00:00:00+00:00", "resolved": False, "resolved_by": None,
             "resolved_at": None, "client": None} for i in range(1, 1001)]
    (layout_root_of / "shg.comments.json").write_text(json.dumps({"version": 1, "next": 1001, "comments": rows}), encoding="utf-8")
    r = ow.client.post("/optical-layouts/shg/comments", json={"text": "full"}, headers=H())
    assert r.status_code == 429 and r.json()["error"] == "layout_comments_full"


# ── the briefing ─────────────────────────────────────────────────────────────────────────────


def test_the_briefing_names_each_layout_with_its_size_and_open_comments(ow):
    _save("shg", "SHG excitation")
    _save("alt", "Alternate", comps=[laser("las1", 0, 0)])
    S.get_store().add_comment("shg", text="a", by="ana", author="operator")
    b = ow.client.get("/briefing", headers=H(), params={"sections": "optical_layouts"}).json()
    sec = b["sections"]["optical_layouts"]
    assert sec["ok"] and sec["data"]["count"] == 2 and {r["id"] for r in sec["data"]["layouts"]} == {"shg", "alt"}
    assert 'shg "SHG excitation"' in sec["text"] and "3 个器件，未解决评论 1 条" in sec["text"]
    assert "mast_optical_layout" in sec["text"] and "mast_optical_layout_comment" in sec["text"]
    assert "## optical_layouts" in b["text"]
    full = ow.client.get("/briefing", headers=H()).json()
    assert "## optical_layouts" in full["text"]


def test_the_briefing_reads_files_only_and_opens_no_device(ow):
    _save()
    ow.client.get("/briefing", headers=H())
    assert ow.state.refreshed == 0 and ow.pool.calls == []


def test_a_layout_section_is_offered_on_both_station_profiles():
    from mast.api.ext.overview import OPTICAL_ORDER, SECTIONS

    assert "optical_layouts" in SECTIONS and "optical_layouts" in OPTICAL_ORDER
    assert list(SECTIONS)[:15] == ["status", "scope", "resume", "tip", "instrument", "live", "prefs", "safety",
                                   "recent_actions", "recent_files", "alarms", "notes", "recording", "jobs",
                                   "operator_requests"], "the STM briefing keeps its sections and their order"


def test_the_ext_surface_for_layouts_is_exactly_these_four_routes(ow):
    spec = ow.app.openapi()["paths"]
    mine = sorted((m.upper(), p) for p, ops in spec.items() if "optical-layouts" in p for m in ops)
    assert mine == [("GET", "/optical-layouts"), ("GET", "/optical-layouts/{layout_id}"),
                    ("GET", "/optical-layouts/{layout_id}/render.png"), ("POST", "/optical-layouts/{layout_id}/comments")]


# ── a trace the budget cut ───────────────────────────────────────────────────────────────────


def test_an_agent_reads_a_cut_trace_with_the_notice_in_the_text(ow, monkeypatch):
    """The agent's detail carries the trace as JSON and the description: a cut shows in both, and the
    text up in its header (the problems are at the end and the text is cut from the end)."""
    from mast.optical_layout import trace as T

    _save(comps=[laser("las1", 50, 62.5), comp("bs1", "beam_splitter", 150, 62.5, 135), mirror("m1", 150, 150, 315),
                 det("d1", 300, 150, 180), det("d2", 300, 62.5, 180)])
    monkeypatch.setattr(T, "MAX_TRACE_EVENTS", 3)
    body = ow.client.get("/optical-layouts/shg", headers=H()).json()
    assert body["trace"][-1]["branches"][-1]["end"] == "budget"
    assert sum(len(b["events"]) for t in body["trace"] for b in t["branches"]) == 3
    assert "追迹被截断" in body["summary"].splitlines()[3] and "追迹在这里被截断" in body["summary"]
    assert ow.client.get("/optical-layouts/shg/render.png", headers=H()).status_code == 200


# ── the length of a comment is declared (411) and bounded (413), before the body is read ─────


def _serve(app):
    import threading
    import time

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    return server, t, server.servers[0].sockets[0].getsockname()[1]


def _ask(port, head, body=b"", *, wait=6.0):
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


def test_a_chunked_comment_is_a_411_and_its_body_is_never_read(ow):
    _save()
    seen = []

    def gen():
        for part in (b'{"text": ', b'"hi"}'):
            seen.append(part)
            yield part

    r = ow.client.post("/optical-layouts/shg/comments", content=gen(), headers={**H("claude"), "Content-Type": "application/json"})
    assert r.status_code == 411 and r.json()["error"] == "length_required" and "Content-Length" in r.json()["detail"]
    assert seen == [], "the server asked for the body of a request it was going to refuse"
    assert S.get_store().comments("shg") == []
    # and with a small length claimed beside a Transfer-Encoding, which would bound nothing
    r = ow.client.post("/optical-layouts/shg/comments", content=b'{"text": "hi"}',
                       headers={**H("claude"), "Content-Type": "application/json", "Transfer-Encoding": "chunked"})
    assert r.status_code == 411 and r.json()["error"] == "length_required"
    assert S.get_store().comments("shg") == []


def test_too_long_a_comment_is_a_413_and_a_bad_length_a_400(ow):
    _save()
    url = "/optical-layouts/shg/comments"
    head = {**H("claude"), "Content-Type": "application/json"}
    for declared, status, code in (("64001", 413, "request_too_large"), ("99999999999", 413, "request_too_large"),
                                   ("abc", 400, "bad_content_length"), ("-1", 400, "bad_content_length")):
        r = ow.client.post(url, content=b'{"text": "hi"}', headers={**head, "Content-Length": declared})
        assert r.status_code == status and r.json()["error"] == code, (declared, r.status_code, r.text)
    assert S.get_store().comments("shg") == []
    # a comment at its longest (2000 characters, each of them three bytes) fits
    assert ow.client.post(url, json={"text": "中" * 2000}, headers=H("claude")).status_code == 200


def test_over_a_real_socket_the_comment_is_refused_before_any_body_is_sent(ow):
    _save()
    server, t, port = _serve(ow.app)
    base = ["POST /optical-layouts/shg/comments HTTP/1.1", f"Host: 127.0.0.1:{port}", "Content-Type: application/json",
            "X-MAST-Actor: claude"]
    try:
        status, raw = _ask(port, base + ["Transfer-Encoding: chunked"])
        assert status == 411, raw[:200]
        status, raw = _ask(port, base + ["Content-Length: 3000000"])
        assert status == 413, raw[:200]
        status, raw = _ask(port, base + ["Transfer-Encoding: chunked", "Content-Length: 5"])
        assert status in (400, 411), raw[:200]
        body = b'{"text": "over a socket"}'
        status, raw = _ask(port, base + [f"Content-Length: {len(body)}"], body)
        assert status == 200, raw[:200]
    finally:
        server.should_exit = True
        t.join(timeout=20)
    assert [c["text"] for c in S.get_store().comments("shg")] == ["over a socket"]


def test_a_request_traces_the_layout_once_and_hands_it_to_the_description(ow, monkeypatch):
    """The agent's detail has the trace and the description made from it; the picture is its own request."""
    from mast.api.ext import optical_layout as ext_mod
    from mast.optical_layout import render as render_mod
    from mast.optical_layout import summary as summary_mod
    from mast.optical_layout import trace as T

    _save()
    calls: list[str] = []
    real = T.trace_layout

    def counting(*a, **k):
        calls.append("trace")
        return real(*a, **k)

    with monkeypatch.context() as m:
        for mod in (ext_mod, render_mod, summary_mod):
            m.setattr(mod, "trace_layout", counting)
        for path in ("/optical-layouts/shg", "/optical-layouts/shg/render.png"):
            calls.clear()
            assert ow.client.get(path, headers=H()).status_code == 200, path
            assert calls == ["trace"], (path, calls)
