"""位点标注页的本地端点：``GET /api/site-marking/{page_id}/page|doc``、``POST .../doc``。

一个只挂这一个 router 的最小 app，``MAST2_PROJECT_ROOT`` 指到 tmp；不碰仪器、不起真 MAST。覆盖：

* 页面：以 text/html 服务、不缓存、只许同源嵌入；页面目录 / page.html 不在 → 404；
* 文档：marks.json 还没有 → 空文档，且读不写；页面目录不在 → 404 而不是空文档，也不凭 URL 建目录；
* 补丁：sites 的 set / update / delete 与四个计划文档的 set（含 null 清空）；update 不替已删除的站位
  建一行；一切不合法的补丁 → 400 且文件**一个字节都不动**；
* 落盘契约（准备页面的技能、本路由、读标记的技能共用）：``<root>/artifacts/site_marking/<page_id>/marks.json``（没有 MASTv2 段），
  UTF-8、不留临时文件、手改文件里多出来的键与 BOM 写一次之后还在；
* marks.json 坏了 → 读写都是 500 并保持原样（绝不折成空文档再被下一次写覆盖）；
* 写闸门：体必填、要 application/json（415）、浏览器请求验 Host / Sec-Fetch-Site / Origin（403）；
  程序化客户端不受影响；
* page_id 路径穿越；并发写入（HTTP 与进程内）不丢更新。

断言只看状态码与结构，不看错误信息的文字（语言随请求变）。
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mast.api.routes import site_marking as sm

PAGE = "p1"
EMPTY = {"sites": {}, "plan/marking": None, "plan/card": None, "plan/confirm": None, "status/live": None}


# ── 夹具与小工具 ─────────────────────────────────────────────────────────


@pytest.fixture()
def root(tmp_path, monkeypatch) -> Path:
    """每个测试一个独立的 project_root；不重定向，写入会落进真实的 artifacts/。"""
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(tmp_path))
    return tmp_path


def pdir(root: Path, page_id: str = PAGE) -> Path:
    """准备页面的技能、本路由、读标记的技能三件约定的页面目录。整个测试文件里只有这里和 test_the_page_directory_is_the_agreed_one 写死这条路径。"""
    return root / "artifacts" / "site_marking" / page_id


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(sm.router, prefix="/api")
    return app


@pytest.fixture()
def client(root) -> TestClient:
    return TestClient(_app())


@pytest.fixture()
def page(root) -> Path:
    """一个已经生成了页面的标注页目录（PrepareSiteMarkingPage 做完的样子）。"""
    d = pdir(root)
    d.mkdir(parents=True)
    (d / "page.html").write_text("<!doctype html><title>t</title><p>标点</p>", encoding="utf-8")
    return d


def url(page_id: str = PAGE, leaf: str = "doc") -> str:
    return f"/api/site-marking/{page_id}/{leaf}"


def site_set(sid: str, **data) -> dict:
    return {"target": "sites", "id": sid, "op": "set", "data": data}


def site_update(sid: str, **data) -> dict:
    return {"target": "sites", "id": sid, "op": "update", "data": data}


def site_delete(sid: str) -> dict:
    return {"target": "sites", "id": sid, "op": "delete"}


def doc_set(path: str, data) -> dict:
    return {"target": "doc", "path": path, "op": "set", "data": data}


def raw_post(client: TestClient, body: bytes, *, ctype: str | None = "application/json", **kw):
    headers = {"Content-Type": ctype} if ctype else {}
    return client.post(url(), content=body, headers=headers, **kw)


# ── 页面 ─────────────────────────────────────────────────────────────────


def test_the_page_is_served_as_html_never_cached_and_only_embeddable_same_origin(client, page):
    r = client.get(url(leaf="page"))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "标点" in r.text and r.content == (page / "page.html").read_bytes()
    assert r.headers["cache-control"] == "no-store"
    # 页面上有「确认任务卡，开始测量」：别的站点不许用 iframe 盖住它诱导点击
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "SAMEORIGIN"


def test_a_missing_page_is_404_whether_the_directory_or_only_the_file_is_gone(client, page):
    assert client.get(url("nope", "page")).status_code == 404
    (page / "page.html").unlink()
    assert client.get(url(leaf="page")).status_code == 404


# ── 读文档 ───────────────────────────────────────────────────────────────


def test_the_doc_is_empty_before_the_first_write_and_reading_never_writes(client, page):
    r = client.get(url())
    assert r.status_code == 200
    assert r.json() == EMPTY
    assert r.headers["cache-control"] == "no-store"
    assert list(r.json()) == list(EMPTY)                    # 键序：sites 在前，页面与 agent 都按名字取
    assert os.listdir(page) == ["page.html"]                 # 读路径不创建 marks.json


def test_the_page_directory_is_the_agreed_one(root):
    """PrepareSiteMarkingPage、本路由、ReadSiteMarks 三件必须指向同一个目录：
    ``project_root()/artifacts/site_marking/<page_id>``，**没有** MASTv2 段。"""
    assert sm.page_dir("abc", create=True) == root / "artifacts" / "site_marking" / "abc"
    assert "MASTv2" not in sm.page_dir("abc").relative_to(root).parts
    assert sm.MARKS_NAME == "marks.json" and sm.PAGE_NAME == "page.html"     # 直接放在目录里，不再套一层


def test_an_unknown_page_is_404_for_both_doc_verbs_and_no_directory_appears(client, root):
    assert client.get(url("ghost")).status_code == 404
    assert client.post(url("ghost"), json=site_set("a", x=1)).status_code == 404
    assert not (root / "artifacts").exists()                 # 凭 URL 建目录会让写错的 page_id 悄悄变成孤儿
    assert "ghost" not in sm._LOCKS                           # 也不为没见过的 id 登记锁：GET 随便什么 id 都不该让注册表变大


# ── sites ────────────────────────────────────────────────────────────────


def test_site_set_update_delete_roundtrip(client, page):
    r = client.post(url(), json=site_set("a", n=1, x=-76.605, y=154.857, kind="auto", note="备注"))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get(url()).json()["sites"] == {
        "a": {"n": 1, "x": -76.605, "y": 154.857, "kind": "auto", "note": "备注"}}

    # update 是浅合并：没提到的键留着，null 写成 null（页面拖动站位时用它作废旧的精确化结果）
    assert client.post(url(), json=site_update("a", rx=1.1, note=None)).json() == {"ok": True}
    assert client.get(url()).json()["sites"]["a"] == {
        "n": 1, "x": -76.605, "y": 154.857, "kind": "auto", "note": None, "rx": 1.1}

    client.post(url(), json=site_set("b", n=2, x=0.0, y=0.0))
    assert list(client.get(url()).json()["sites"]) == ["a", "b"]

    assert client.post(url(), json=site_delete("a")).json() == {"ok": True}
    assert list(client.get(url()).json()["sites"]) == ["b"]
    # 删除幂等：重试（上一次的响应丢了）和删一个从没有过的 id 都不是错误
    assert client.post(url(), json=site_delete("a")).status_code == 200
    assert client.post(url(), json=site_delete("never-existed")).status_code == 200
    assert list(client.get(url()).json()["sites"]) == ["b"]


def test_set_replaces_the_whole_site_it_does_not_merge(client, page):
    client.post(url(), json=site_set("a", x=1, y=2, note="旧"))
    client.post(url(), json=site_set("a", x=5))
    assert client.get(url()).json()["sites"]["a"] == {"x": 5}


def test_update_of_a_missing_site_is_404_and_does_not_resurrect_a_deleted_one(client, page):
    assert client.post(url(), json=site_update("ghost", status="done")).status_code == 404
    client.post(url(), json=site_set("a", x=1, y=2))
    client.post(url(), json=site_delete("a"))
    # 用户刚删掉 a，agent 随后回写 a 的 status：不能复活成一行没有坐标的残骸
    assert client.post(url(), json=site_update("a", status="done")).status_code == 404
    assert client.get(url()).json()["sites"] == {}


# ── 计划 / 状态文档 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("path", sm.DOC_PATHS)
def test_each_plan_doc_sets_and_clears_without_touching_anything_else(client, page, path):
    client.post(url(), json=site_set("a", x=1, y=2))
    others = {p: {"keep": p} for p in sm.DOC_PATHS if p != path}
    for p, data in others.items():
        assert client.post(url(), json=doc_set(p, data)).status_code == 200

    body = {"version": 1, "blocks": [{"h": "位点", "items": ["1", "2"]}], "done": True}
    assert client.post(url(), json=doc_set(path, body)).json() == {"ok": True}
    doc = client.get(url()).json()
    assert doc[path] == body
    assert doc["sites"] == {"a": {"x": 1, "y": 2}}
    assert all(doc[p] == d for p, d in others.items())

    assert client.post(url(), json=doc_set(path, None)).status_code == 200      # null = 清空
    doc = client.get(url()).json()
    assert doc[path] is None
    assert doc["sites"] == {"a": {"x": 1, "y": 2}} and all(doc[p] == d for p, d in others.items())


# ── 落盘 ─────────────────────────────────────────────────────────────────


def test_marks_json_lands_in_the_page_dir_as_utf8_and_leaves_no_temp_files(client, page):
    for i in range(5):
        client.post(url(), json=site_set(f"s{i}", n=i, note="位点备注"))
    client.post(url(), json=doc_set("status/live", {"done": 2, "total": 5}))

    on_disk = (page / "marks.json").read_text(encoding="utf-8")
    assert "位点备注" in on_disk                              # 不是 \uXXXX 转义：人打开文件能直接读
    assert json.loads(on_disk) == client.get(url()).json()
    assert sorted(os.listdir(page)) == ["marks.json", "page.html"]


def test_the_empty_document_prepare_site_marking_page_leaves_behind_is_a_valid_start(client, page):
    """PrepareSiteMarkingPage 预先写一份紧凑的空文档当 marks.json —— 契约的另一端，钉住它能被读写。"""
    (page / "marks.json").write_text(json.dumps(EMPTY, ensure_ascii=False), encoding="utf-8")
    assert client.get(url()).json() == EMPTY
    assert client.post(url(), json=site_set("a", x=1)).status_code == 200
    assert client.get(url()).json()["sites"] == {"a": {"x": 1}}


def test_extra_keys_and_a_bom_in_a_hand_edited_file_survive_a_write(client, page):
    hand = {"sites": {"s": {"x": 1}}, "extra": {"keep": [1, 2, 3]}}
    (page / "marks.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(hand).encode("utf-8"))
    doc = client.get(url()).json()
    assert doc["sites"] == {"s": {"x": 1}} and doc["extra"] == {"keep": [1, 2, 3]}
    assert doc["plan/card"] is None                          # 缺的标准键补齐

    client.post(url(), json=doc_set("plan/marking", {"done": True}))
    again = json.loads((page / "marks.json").read_text(encoding="utf-8"))
    assert again["extra"] == {"keep": [1, 2, 3]} and again["sites"] == {"s": {"x": 1}}
    assert again["plan/marking"] == {"done": True}


# ── 补丁校验 ─────────────────────────────────────────────────────────────

_BAD_PATCHES = [
    pytest.param({}, id="empty-object"),
    pytest.param({"op": "set", "id": "a", "data": {}}, id="no-target"),
    pytest.param({"target": "nope", "id": "a", "op": "set", "data": {}}, id="unknown-target"),
    pytest.param({"target": ["sites"], "id": "a", "op": "set", "data": {}}, id="target-not-a-string"),
    pytest.param({"target": "sites", "id": "a", "op": "merge", "data": {}}, id="unknown-site-op"),
    pytest.param({"target": "sites", "id": "a", "data": {}}, id="no-op"),
    pytest.param({"target": "sites", "op": "set", "data": {}}, id="site-without-id"),
    pytest.param({"target": "sites", "id": "", "op": "set", "data": {}}, id="empty-id"),
    pytest.param({"target": "sites", "id": "   ", "op": "set", "data": {}}, id="blank-id"),
    pytest.param({"target": "sites", "id": 5, "op": "set", "data": {}}, id="numeric-id"),
    pytest.param({"target": "sites", "id": "x" * 129, "op": "set", "data": {}}, id="id-too-long"),
    pytest.param({"target": "sites", "id": "a\nb", "op": "set", "data": {}}, id="control-char-in-id"),
    pytest.param({"target": "sites", "id": "a", "op": "set"}, id="set-without-data"),
    pytest.param({"target": "sites", "id": "a", "op": "update"}, id="update-without-data"),
    pytest.param({"target": "sites", "id": "a", "op": "set", "data": [1]}, id="site-data-list"),
    pytest.param({"target": "sites", "id": "a", "op": "set", "data": "x"}, id="site-data-string"),
    pytest.param({"target": "sites", "id": "a", "op": "set", "data": None}, id="site-data-null"),
    pytest.param({"target": "sites", "id": "a", "op": "set", "data": {}, "if_version": 3}, id="stray-if_version"),
    pytest.param({"target": "sites", "id": "a", "op": "set", "data": {}, "path": "plan/card"}, id="site-with-path"),
    pytest.param({"target": "doc", "path": "plan/other", "op": "set", "data": {}}, id="unknown-doc-path"),
    pytest.param({"target": "doc", "path": ["plan/card"], "op": "set", "data": {}}, id="doc-path-not-a-string"),
    pytest.param({"target": "doc", "op": "set", "data": {}}, id="doc-without-path"),
    pytest.param({"target": "doc", "path": "plan/card", "op": "update", "data": {}}, id="doc-update"),
    pytest.param({"target": "doc", "path": "plan/card", "op": "delete"}, id="doc-delete"),
    pytest.param({"target": "doc", "path": "plan/card", "op": "set"}, id="doc-set-without-data"),
    pytest.param({"target": "doc", "path": "plan/card", "op": "set", "data": [1]}, id="doc-data-list"),
    pytest.param({"target": "doc", "path": "plan/card", "op": "set", "data": 3}, id="doc-data-number"),
    pytest.param({"target": "doc", "path": "plan/card", "op": "set", "data": {}, "id": "a"}, id="doc-with-id"),
]


@pytest.mark.parametrize("patch", _BAD_PATCHES)
def test_every_malformed_patch_is_400_and_the_file_is_untouched(client, page, patch):
    client.post(url(), json=site_set("keep", x=1))
    before = (page / "marks.json").read_bytes()
    r = client.post(url(), json=patch)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]
    assert (page / "marks.json").read_bytes() == before


def test_error_details_do_not_echo_a_huge_request_value_back(client, page):
    for patch in ({"target": "sites", "id": "a", "op": "set", "data": {}, "k" * 5000: 1},
                  {"target": "x" * 5000},
                  {"target": "sites", "id": "a", "op": "y" * 5000, "data": {}},
                  {"target": "doc", "path": "z" * 5000, "op": "set", "data": {}}):
        r = client.post(url(), json=patch)
        assert r.status_code == 400 and len(r.json()["detail"]) < 400


@pytest.mark.parametrize("body", [b"[]", b'"x"', b"1", b"null", b"true", b"{not json", b"\xff\xfe", b""],
                         ids=["list", "string", "number", "null", "bool", "broken", "not-utf8", "empty"])
def test_a_body_that_is_not_a_json_object_is_400_and_a_missing_body_is_too(client, page, body):
    assert raw_post(client, body).status_code == 400
    assert raw_post(client, b"  \n").status_code == 400      # 只有空白也算没有体
    assert not (page / "marks.json").exists()


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity", b"1e999"])
def test_non_finite_numbers_are_400_and_never_reach_the_file(client, page, literal):
    body = b'{"target":"sites","id":"a","op":"set","data":{"x":' + literal + b"}}"
    assert raw_post(client, body).status_code == 400
    assert not (page / "marks.json").exists()                # 写出去的 NaN 浏览器的 JSON.parse 读不了


def test_an_oversized_patch_is_413_declared_or_streamed(client, page, monkeypatch):
    monkeypatch.setattr(sm, "_MAX_BODY_BYTES", 2500)
    big = json.dumps(site_set("a", note="x" * 3000)).encode("utf-8")
    assert raw_post(client, big).status_code == 413           # 声明了 Content-Length

    def chunks():                                             # 没有 Content-Length：分块上传
        for _ in range(5):
            yield b" " * 1000
    r = client.post(url(), content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert not (page / "marks.json").exists()


def test_a_write_that_would_push_the_document_over_its_limit_is_413_and_changes_nothing(client, page, monkeypatch):
    monkeypatch.setattr(sm, "_MAX_DOC_BYTES", 600)
    assert client.post(url(), json=site_set("a", x=1)).status_code == 200
    before = (page / "marks.json").read_bytes()
    assert client.post(url(), json=site_set("b", note="y" * 800)).status_code == 413
    assert (page / "marks.json").read_bytes() == before


# ── 写闸门 ───────────────────────────────────────────────────────────────


def test_a_write_needs_a_json_content_type_and_a_body(client, page):
    ok = json.dumps(site_set("a", x=1))
    assert client.post(url()).status_code == 415                                   # no-cors 的空 POST
    assert client.post(url(), content=ok.encode()).status_code == 415              # 没有 Content-Type
    assert client.post(url(), content=ok.encode(), headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post(url(), data={"target": "sites"}).status_code == 415         # 表单
    assert not (page / "marks.json").exists()

    for ctype in ("application/json", "application/json; charset=utf-8", "application/merge-patch+json"):
        assert raw_post(client, ok.encode(), ctype=ctype).status_code == 200, ctype
    # 空体 + JSON 头：通过闸门，倒在「体必填」上
    assert raw_post(client, b"").status_code == 400


@pytest.mark.parametrize("base, headers", [
    ("http://127.0.0.1:7862", {"Origin": "http://127.0.0.1:7862", "Sec-Fetch-Site": "same-origin"}),
    ("http://localhost:7862", {"Origin": "http://localhost:7862"}),
    ("http://100.64.77.66:7862", {"Origin": "http://100.64.77.66:7862", "Sec-Fetch-Site": "same-origin"}),
    ("https://127.0.0.1:7862", {"Origin": "https://127.0.0.1:7862"}),
    ("http://127.0.0.1:7862", {"Sec-Fetch-Site": "none"}),                  # 用户在地址栏里自己敲的
    ("http://rebind.example:7862", {}),                                      # 程序化客户端按主机名也行
], ids=["loopback-ip", "localhost", "tailscale-ip", "https", "typed-url", "programmatic-by-name"])
def test_same_origin_browsers_and_programmatic_clients_may_write(root, page, base, headers):
    c = TestClient(_app(), base_url=base)
    r = c.post(url(), json=site_set("a", x=1), headers=headers)
    assert r.status_code == 200, r.text
    assert c.get(url()).json()["sites"] == {"a": {"x": 1}}


@pytest.mark.parametrize("base, headers", [
    ("http://127.0.0.1:7862", {"Origin": "http://evil.example"}),
    ("http://127.0.0.1:7862", {"Origin": "http://127.0.0.1:7863"}),          # 同机别的端口上的网页
    ("http://127.0.0.1:7862", {"Origin": "https://127.0.0.1:7862"}),         # scheme 不同
    ("http://127.0.0.1:7862", {"Origin": "null"}),                           # 沙箱 iframe / file://
    ("http://127.0.0.1:7862", {"Origin": "http://[::1"}),                    # 畸形
    ("http://127.0.0.1:7862", {"Sec-Fetch-Site": "cross-site"}),
    ("http://127.0.0.1:7862", {"Sec-Fetch-Site": "same-site"}),
    ("http://127.0.0.1:7862", {"Origin": "http://evil.example", "Sec-Fetch-Site": "same-origin"}),
    ("http://rebind.example:7862", {"Origin": "http://rebind.example:7862"}),  # DNS 重绑定：Origin 与 Host 一致，但 Host 是陌生主机名
    ("http://rebind.example:7862", {"Sec-Fetch-Site": "same-origin"}),
], ids=["foreign-origin", "other-port", "other-scheme", "null-origin", "malformed-origin", "cross-site",
        "same-site", "origin-beats-sec-fetch", "dns-rebinding", "dns-rebinding-no-origin"])
def test_cross_origin_browser_writes_are_403_and_change_nothing(root, page, base, headers):
    c = TestClient(_app(), base_url=base)
    r = c.post(url(), json=site_set("a", x=1), headers=headers)
    assert r.status_code == 403, r.text
    assert not (page / "marks.json").exists()


def _guard(host: str, *, origin: str | None = None, scheme: str = "http") -> tuple[int, str] | None:
    """直接问闸门。Starlette 的 TestClient 解析不了 IPv6 的 base_url，这一族只能绕过它构造请求。"""
    from starlette.requests import Request

    headers = [(b"host", host.encode()), (b"content-type", b"application/json")]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return sm._write_refusal(Request({
        "type": "http", "method": "POST", "scheme": scheme, "path": "/api/site-marking/p1/doc",
        "raw_path": b"/api/site-marking/p1/doc", "query_string": b"", "headers": headers,
        "server": ("::1", 7862)}))


def test_the_ipv6_loopback_is_a_valid_host_and_origin_but_only_for_itself():
    assert _guard("[::1]:7862", origin="http://[::1]:7862") is None
    assert _guard("[::1]:7862") is None                                  # 程序化客户端
    assert _guard("[::1]:7862", origin="http://[::1]:7863")[0] == 403    # 同机别的端口
    assert _guard("[::1]:7862", origin="http://127.0.0.1:7862")[0] == 403  # 同一台机器，但不是同一个源
    assert _guard("[fd7a:115c::1]:7862", origin="http://[fd7a:115c::1]:7862") is None   # Tailscale IPv6


# ── page_id ──────────────────────────────────────────────────────────────

_BAD_IDS = ["%2E%2E", "a%5Cb", "..%5C..%5Cx", "a.b", "con", "NUL", "Com1", "x" * 65, "%20", "a%20b",
            "%E9%A1%B5", "a%00b", "ok%0A", "a%3Ab", "a%2Bb"]


@pytest.mark.parametrize("bad", _BAD_IDS)
def test_a_malformed_page_id_is_400_on_every_endpoint_and_creates_nothing(client, root, bad):
    assert client.get(url(bad, "page")).status_code == 400
    assert client.get(url(bad)).status_code == 400
    assert client.post(url(bad), json=site_set("a", x=1)).status_code == 400
    assert not (root / "artifacts").exists()


def test_an_encoded_slash_never_reaches_a_handler_and_creates_nothing(client, root):
    for leaf in ("page", "doc"):
        assert client.get(f"/api/site-marking/..%2F..%2Fescape/{leaf}").status_code in (400, 404)
    assert client.post("/api/site-marking/..%2F..%2Fescape/doc", json=site_set("a", x=1)).status_code in (400, 404)
    assert not (root / "artifacts").exists() and not (root.parent / "escape").exists()


@pytest.mark.parametrize("good", ["abc", "A-b_9", "x" * 64, "0"])
def test_ordinary_page_ids_are_accepted(client, root, good):
    sm.page_dir(good, create=True)
    assert client.post(url(good), json=site_set("a", x=1)).status_code == 200
    assert client.get(url(good)).json()["sites"] == {"a": {"x": 1}}
    assert (pdir(root, good) / "marks.json").is_file()


# ── marks.json 坏了 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("junk", [
    b"{not json", b"[]", b'"x"', b'{"sites": []}', b'{"sites": 3}', b"\xff\xfe\x00",
    b'{"sites": {}, "plan/card": NaN}', b'{"sites": {"a": 1e999}}',
], ids=["broken", "list", "string", "sites-list", "sites-number", "not-utf8", "nan", "overflow"])
def test_a_corrupt_marks_json_is_a_loud_500_for_reads_and_writes_and_stays_as_it_was(client, page, junk):
    (page / "marks.json").write_bytes(junk)
    assert client.get(url()).status_code == 500                # 不能折成空文档
    assert client.post(url(), json=site_set("a", x=1)).status_code == 500
    assert (page / "marks.json").read_bytes() == junk          # 也没被「顺手修好」：里面可能是操作员的全部标记
    assert sorted(os.listdir(page)) == ["marks.json", "page.html"]

    (page / "marks.json").unlink()                             # 人工处理之后恢复
    assert client.get(url()).json() == EMPTY
    assert client.post(url(), json=site_set("a", x=1)).status_code == 200


@pytest.mark.parametrize("blank", [b"", b"  \r\n\t"])
def test_a_blank_marks_json_counts_as_absent_there_is_nothing_in_it_to_lose(client, page, blank):
    (page / "marks.json").write_bytes(blank)
    assert client.get(url()).json() == EMPTY
    assert client.post(url(), json=site_set("a", x=1)).status_code == 200
    assert json.loads((page / "marks.json").read_text(encoding="utf-8"))["sites"] == {"a": {"x": 1}}


def test_a_null_sites_member_reads_as_no_sites(client, page):
    (page / "marks.json").write_text('{"sites": null}', encoding="utf-8")
    assert client.get(url()).json() == EMPTY


def test_updating_a_stored_site_that_is_not_an_object_is_500_and_changes_nothing(client, page):
    (page / "marks.json").write_text('{"sites": {"a": 5}}', encoding="utf-8")
    before = (page / "marks.json").read_bytes()
    assert client.post(url(), json=site_update("a", x=1)).status_code == 500
    assert (page / "marks.json").read_bytes() == before


def test_a_file_that_stays_locked_is_503_not_a_lost_write(client, page, monkeypatch):
    client.post(url(), json=site_set("a", x=1))
    before = (page / "marks.json").read_bytes()

    def locked(path, data):
        raise PermissionError("held by another program")
    monkeypatch.setattr(sm, "atomic_write_bytes", locked)
    assert client.post(url(), json=site_update("a", x=2)).status_code == 503     # 页面据此把改动留在本页，稍后重发
    assert (page / "marks.json").read_bytes() == before
    assert sorted(os.listdir(page)) == ["marks.json", "page.html"]


# ── 并发 ─────────────────────────────────────────────────────────────────


def test_concurrent_http_writers_lose_no_updates(root, page):
    c = TestClient(_app())

    def worker(t: int) -> None:
        assert c.post(url(), json=site_set(f"t{t}", n=t)).status_code == 200
        for i in range(8):
            assert c.post(url(), json=site_update(f"t{t}", **{f"f{i}": i})).status_code == 200

    with ThreadPoolExecutor(max_workers=6) as pool:
        for fut in [pool.submit(worker, t) for t in range(6)]:
            fut.result()
    sites = c.get(url()).json()["sites"]
    assert sorted(sites) == [f"t{t}" for t in range(6)]
    for t in range(6):
        assert sites[f"t{t}"] == {"n": t, **{f"f{i}": i for i in range(8)}}


def test_concurrent_in_process_writers_and_readers_lose_no_updates_and_never_see_a_torn_file(root, page):
    """skill（agent 回写）与页面（用户拖动）同时写同一页：读-改-写必须是整体的。"""
    writers, rounds = 8, 25
    start = threading.Barrier(writers + 1)
    stop = threading.Event()
    seen: list[BaseException] = []

    def write(t: int) -> None:
        start.wait()
        sm.apply_patch(PAGE, site_set(f"w{t}", n=t))
        for i in range(rounds):
            sm.apply_patch(PAGE, site_update(f"w{t}", **{f"f{i}": i}))

    def read() -> None:
        start.wait()
        while not stop.is_set():
            try:
                sm.read_doc(PAGE)
            except BaseException as exc:            # noqa: BLE001 —— 任何异常都是失败：读到半截文件或 PermissionError
                seen.append(exc)
                return
            time.sleep(0.002)                        # 页面是 1.2 s 一轮，这里已经比它狠得多；别饿死写入方

    threads = [threading.Thread(target=write, args=(t,)) for t in range(writers)]
    reader = threading.Thread(target=read)
    for th in (*threads, reader):
        th.start()
    for th in threads:
        th.join(60)
    stop.set()
    reader.join(10)

    assert not seen, seen
    sites = sm.read_doc(PAGE)["sites"]
    assert sorted(sites) == sorted(f"w{t}" for t in range(writers))
    for t in range(writers):
        assert sites[f"w{t}"] == {"n": t, **{f"f{i}": i for i in range(rounds)}}
    assert sorted(os.listdir(page)) == ["marks.json", "page.html"]


# ── 进程内接口（skill 用） ───────────────────────────────────────────────


def test_the_in_process_api_matches_the_http_one_and_raises_with_a_status(root, page):
    sm.apply_patch(PAGE, site_set("a", x=1))
    sm.apply_patch(PAGE, doc_set("plan/marking", {"done": True}))
    doc = sm.read_doc(PAGE)
    assert doc["sites"] == {"a": {"x": 1}} and doc["plan/marking"] == {"done": True}
    assert set(doc) == set(EMPTY)

    for call, status in [
        (lambda: sm.apply_patch(PAGE, site_update("ghost", x=1)), 404),
        (lambda: sm.apply_patch(PAGE, {"target": "nope"}), 400),
        (lambda: sm.apply_patch(PAGE, site_set("a", bad=object())), 400),          # 存不成 JSON 的对象
        (lambda: sm.apply_patch("zzz", site_set("a", x=1)), 404),
        (lambda: sm.read_doc("zzz"), 404),
        (lambda: sm.page_dir("zzz"), 404),
        (lambda: sm.apply_patch("../x", site_set("a", x=1)), 400),
        (lambda: sm.read_doc("../x"), 400),
        (lambda: sm.page_dir("../x", create=True), 400),
    ]:
        with pytest.raises(sm.SiteMarkingError) as ei:
            call()
        assert ei.value.status == status
    assert sm.read_doc(PAGE)["sites"] == {"a": {"x": 1}}                              # 失败的补丁一个字节都没动
    assert not pdir(root, "zzz").exists()                                             # 也没凭 page_id 建目录

    made = sm.page_dir("fresh", create=True)
    assert made == pdir(root, "fresh") and made.is_dir()
    assert sm.read_doc("fresh") == EMPTY


def test_the_page_lock_is_shared_across_letter_case_because_ntfs_is_case_insensitive():
    assert sm._lock_for("Abc") is sm._lock_for("aBC")
    assert sm._lock_for("abc") is not sm._lock_for("abd")
