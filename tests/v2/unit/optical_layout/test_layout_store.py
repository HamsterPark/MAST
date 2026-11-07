"""The files of the layouts: optimistic revisions, atomic writes, history, archive, comments.

The store is where a drawing survives a restart. What is pinned: a stale save changes nothing, a
failed write leaves both files as they were, a file that cannot be read is never overwritten, the
history keeps the newest 200 versions, a layout is archived and not deleted, and the comment limits
hold (with resolved comments making room for new ones).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from mast.optical_layout import store as S
from mast.optical_layout.model import Layout

from ._layouts import det, laser, layout, mirror


def L(lid="main", n=2, **kw) -> Layout:
    comps = [laser("las1", 50, 62.5), mirror("m1", 112.5, 62.5, 135), det("d", 112.5, 300, 270)][:max(n, 1)]
    return Layout.model_validate(layout(comps, lid=lid, title=kw.pop("title", "T"), **kw))


@pytest.fixture
def st(layout_root) -> S.LayoutStore:
    return S.LayoutStore()


def disk(layout_root: Path, *parts) -> Path:
    return layout_root.joinpath("config", "optical_layouts", *parts)


# ── where it lives ───────────────────────────────────────────────────────────────────────────


def test_the_default_root_is_under_the_project_root_and_follows_it(layout_root, monkeypatch, tmp_path):
    assert S.layout_root() == layout_root / "config" / "optical_layouts"
    S.get_store().save(L(), base_revision=0, by="ana")
    assert disk(layout_root, "main.json").is_file()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("MAST2_PROJECT_ROOT", str(other))
    assert S.get_store().list_layouts() == [], "the root is looked up at every use"


def test_an_explicit_root_is_used(tmp_path):
    mine = S.LayoutStore(tmp_path / "x")
    mine.save(L(), base_revision=0, by="ana")
    assert (tmp_path / "x" / "main.json").is_file()


# ── saving and the revision rule ─────────────────────────────────────────────────────────────


def test_the_first_save_is_revision_one_and_the_server_writes_the_bookkeeping(st, layout_root):
    mine = L()
    mine = mine.model_copy(update={"revision": 99, "updated_by": "forged", "updated_at": "1999"})
    saved = st.save(mine, base_revision=0, by="ana")
    assert saved.revision == 1 and saved.updated_by == "ana" and saved.updated_at.endswith("+00:00")
    assert st.load("main") == saved
    on_disk = json.loads(disk(layout_root, "main.json").read_text(encoding="utf-8"))
    assert on_disk["revision"] == 1 and on_disk["updated_by"] == "ana" and on_disk["schema"] == 1


def test_a_save_must_start_from_the_stored_revision(st):
    st.save(L(), base_revision=0, by="ana")
    with pytest.raises(S.Conflict) as exc:
        st.save(L(title="other"), base_revision=0, by="bob")                  # "new" but it exists
    assert exc.value.current_revision == 1 and exc.value.updated_by == "ana"
    with pytest.raises(S.Conflict):
        st.save(L(title="other"), base_revision=5, by="bob")
    assert st.load("main").title == "T" and st.load("main").revision == 1, "a refused save changes nothing"
    assert st.save(L(title="second"), base_revision=1, by="bob").revision == 2
    with pytest.raises(S.Conflict):
        st.save(L(title="third"), base_revision=1, by="ana")                  # the second tab, stale


def test_a_save_to_a_layout_that_does_not_exist_with_a_nonzero_base_is_a_conflict(st):
    with pytest.raises(S.Conflict) as exc:
        st.save(L(), base_revision=3, by="ana")
    assert exc.value.current_revision == 0 and not st.exists("main")


def test_two_threads_saving_from_the_same_base_one_wins_and_one_conflicts(st):
    st.save(L(), base_revision=0, by="ana")
    results: list = []

    def go(name):
        try:
            results.append(st.save(L(title=name), base_revision=1, by=name).revision)
        except S.Conflict:
            results.append("conflict")

    threads = [threading.Thread(target=go, args=(f"t{i}",)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(map(str, results)) == sorted(["2"] + ["conflict"] * 7), results
    assert [r["revision"] for r in st.history("main", 50)] == [2, 1]


def test_the_listing_has_the_counts_and_puts_the_newest_save_first(st):
    st.save(L("a", title="First"), base_revision=0, by="ana")
    st.save(L("b", title="Second", n=3), base_revision=0, by="bob")
    st.add_comment("b", text="hello", by="bob", author="operator")
    st.add_comment("b", text="done", by="bob", author="operator")
    st.resolve_comment("b", "c2", by="bob")
    rows = st.list_layouts()
    assert [r["id"] for r in rows] == ["b", "a"]
    assert rows[0] == {"id": "b", "title": "Second", "revision": 1, "updated_at": rows[0]["updated_at"],
                       "updated_by": "bob", "components": 3, "open_comments": 1}
    assert rows[1]["components"] == 2 and rows[1]["open_comments"] == 0


def test_a_listing_does_not_revalidate_files_that_have_not_changed(st, layout_root, monkeypatch):
    """The briefing lists the layouts on every call; each file is read once, and again when it changes."""
    st.save(L("a"), base_revision=0, by="ana")
    st.save(L("b", title="B"), base_revision=0, by="ana")
    st.list_layouts()
    calls: list[str] = []
    real = S.LayoutStore.load

    def counting(self, lid):
        calls.append(lid)
        return real(self, lid)

    with monkeypatch.context() as m:
        m.setattr(S.LayoutStore, "load", counting)
        assert [r["id"] for r in st.list_layouts()] == ["b", "a"] and calls == []
        st.save(L("a", title="new"), base_revision=1, by="bob")
        calls.clear()
        rows = {r["id"]: r for r in st.list_layouts()}
        assert rows["a"]["title"] == "new" and rows["a"]["revision"] == 2 and calls == ["a"], "only the changed file was read again"
        # a hand edit (a different size) is noticed too, and a broken file is never remembered
        path = disk(layout_root, "b.json")
        path.write_bytes(path.read_bytes().replace(b'"title": "B"', b'"title": "Bee"'))
        calls.clear()
        assert {r["id"]: r for r in st.list_layouts()}["b"]["title"] == "Bee" and calls == ["b"]
        path.write_bytes(b"{broken")
        assert {r["id"]: r for r in st.list_layouts()}["b"]["error"]
        assert {r["id"]: r for r in st.list_layouts()}["b"]["error"] and calls.count("b") == 3
        path.unlink()
        assert [r["id"] for r in st.list_layouts()] == ["a"]


def test_an_invalid_id_is_simply_not_found(st):
    for bad in ("", "../x", "a/b", "a b", "con", "x" * 41):
        with pytest.raises(S.NotFound):
            st.load(bad)
        assert st.exists(bad) is False


# ── nothing half written ─────────────────────────────────────────────────────────────────────


def test_a_failed_write_changes_neither_the_layout_nor_its_history(st, layout_root, monkeypatch):
    st.save(L(), base_revision=0, by="ana")
    before = (disk(layout_root, "main.json").read_bytes(), disk(layout_root, "history", "main.jsonl").read_bytes())
    real = S.atomic_write

    def boom(path, data):
        if path.name == "main.json":
            raise PermissionError("locked")
        return real(path, data)

    with monkeypatch.context() as m:
        m.setattr(S, "atomic_write", boom)
        with pytest.raises(S.SaveFailed):
            st.save(L(title="lost"), base_revision=1, by="bob")
    after = (disk(layout_root, "main.json").read_bytes(), disk(layout_root, "history", "main.jsonl").read_bytes())
    assert after == before, "the history line of the failed save was taken back"
    assert st.save(L(title="retry"), base_revision=1, by="bob").revision == 2


def test_a_history_that_cannot_be_appended_stops_the_save_before_the_layout_is_touched(st, layout_root, monkeypatch):
    st.save(L(), base_revision=0, by="ana")
    cur = disk(layout_root, "main.json").read_bytes()
    real_open = open

    def refuse(path, mode="r", *a, **k):
        if str(path).endswith("main.jsonl") and "a" in mode:
            raise OSError("disk full")
        return real_open(path, mode, *a, **k)

    with monkeypatch.context() as m:
        m.setattr("builtins.open", refuse)
        with pytest.raises(S.SaveFailed):
            st.save(L(title="lost"), base_revision=1, by="bob")
    assert disk(layout_root, "main.json").read_bytes() == cur


def test_no_temp_file_is_left_and_none_is_called_part(st, layout_root):
    st.save(L(), base_revision=0, by="ana")
    st.add_comment("main", text="x", by="ana", author="operator")
    names = [p.name for p in disk(layout_root).rglob("*") if p.is_file()]
    assert sorted(names) == ["main.comments.json", "main.json", "main.jsonl"]
    assert not [n for n in names if ".tmp" in n or ".part" in n]


# ── a file that cannot be read is never overwritten ──────────────────────────────────────────


@pytest.mark.parametrize("content", [b"{not json", b"[]", b'{"schema": 2, "id": "main"}', b"", b"\xff\xfe\x00",
                                     b'{"schema": 1, "id": "other", "components": []}'])
def test_a_broken_file_is_reported_and_left_exactly_as_it_is(st, layout_root, content, caplog):
    path = disk(layout_root, "main.json")
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    with caplog.at_level("WARNING"):
        with pytest.raises(S.Corrupt) as exc:
            st.load("main")
    assert "main.json" in str(exc.value) and path.read_bytes() == content
    assert any("main.json" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)
    with pytest.raises(S.Corrupt):
        st.save(L(), base_revision=0, by="ana")
    with pytest.raises(S.Corrupt):
        st.archive("main", base_revision=1, by="ana")
    assert path.read_bytes() == content, "neither a save nor an archive touched it"
    rows = st.list_layouts()
    assert rows[0]["id"] == "main" and rows[0]["error"], "it is listed, with the reason"


def test_a_file_with_a_byte_order_mark_is_read(st, layout_root):
    st.save(L(), base_revision=0, by="ana")
    path = disk(layout_root, "main.json")
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert st.load("main").revision == 1


def test_a_stray_file_in_the_folder_is_not_a_layout(st, layout_root):
    st.save(L(), base_revision=0, by="ana")
    disk(layout_root, "notes.txt").write_text("hi")
    disk(layout_root, "has space.json").write_text("{}")
    disk(layout_root, "a.b.json").write_text("{}")
    assert [r["id"] for r in st.list_layouts()] == ["main"]


# ── history ──────────────────────────────────────────────────────────────────────────────────


def test_every_save_appends_a_whole_version_and_old_ones_can_be_read(st):
    for i in range(1, 4):
        st.save(L(title=f"v{i}"), base_revision=i - 1, by=f"u{i}", client={"peer": "127.0.0.1"})
    rows = st.history("main", 10)
    assert [(r["revision"], r["updated_by"], r["title"], r["components"]) for r in rows] == [
        (3, "u3", "v3", 2), (2, "u2", "v2", 2), (1, "u1", "v1", 2)]
    assert st.revision("main", 2).title == "v2" and st.revision("main", 2).revision == 2
    assert [r["revision"] for r in st.history("main", 2)] == [3, 2]
    with pytest.raises(S.NotFound):
        st.revision("main", 9)
    with pytest.raises(S.NotFound):
        st.history("nope")


def test_only_the_newest_200_versions_are_kept(st, layout_root):
    st.save(L(), base_revision=0, by="ana")
    for i in range(1, 204):
        st.save(L(title=f"v{i + 1}"), base_revision=i, by="ana")
    rows = st.history("main", 500)
    assert len(rows) == 200 and rows[0]["revision"] == 204 and rows[-1]["revision"] == 5
    assert len(disk(layout_root, "history", "main.jsonl").read_bytes().splitlines()) == 200
    with pytest.raises(S.NotFound):
        st.revision("main", 4)
    assert st.revision("main", 5).title == "v5"
    assert st.load("main").revision == 204


def test_the_history_records_what_the_server_saw_of_each_writer(st, layout_root):
    st.save(L(), base_revision=0, by="ana", client={"peer": "127.0.0.1", "browser": True})
    line = json.loads(disk(layout_root, "history", "main.jsonl").read_text(encoding="utf-8"))
    assert line["client"] == {"peer": "127.0.0.1", "browser": True} and line["by"] == "ana" and line["revision"] == 1


def test_an_unreadable_history_line_is_skipped_not_fatal(st, layout_root):
    st.save(L(), base_revision=0, by="ana")
    with open(disk(layout_root, "history", "main.jsonl"), "ab") as fh:
        fh.write(b"garbage\n{}\n")
    assert [r["revision"] for r in st.history("main")] == [1]


# ── archive ──────────────────────────────────────────────────────────────────────────────────


def test_archive_moves_the_three_files_and_frees_the_id(st, layout_root):
    st.save(L(), base_revision=0, by="ana")
    st.add_comment("main", text="note", by="ana", author="operator")
    dest = st.archive("main", base_revision=1, by="bob", client={"peer": "x"})
    assert dest.parent == disk(layout_root, "archive") and dest.is_file()
    assert not st.exists("main") and st.list_layouts() == []
    names = sorted(p.name for p in disk(layout_root, "archive").iterdir())
    assert len(names) == 4 and {n.split("-")[0] for n in names} == {"main"}
    assert any(n.endswith(".history.jsonl") for n in names) and any(n.endswith(".comments.json") for n in names)
    meta = json.loads(next(p for p in disk(layout_root, "archive").iterdir() if p.name.endswith(".meta.json")).read_text("utf-8"))
    assert meta["archived_by"] == "bob" and meta["revision"] == 1
    with pytest.raises(S.NotFound):
        st.load("main")
    assert st.save(L(title="again"), base_revision=0, by="ana").revision == 1, "the id can be used again, from revision 1"
    assert st.comments("main") == []


def test_archive_needs_the_current_revision_and_an_existing_layout(st):
    st.save(L(), base_revision=0, by="ana")
    with pytest.raises(S.Conflict):
        st.archive("main", base_revision=0, by="bob")
    assert st.exists("main")
    with pytest.raises(S.NotFound):
        st.archive("nope", base_revision=1, by="bob")


def test_a_failed_archive_puts_everything_back(st, layout_root, monkeypatch):
    st.save(L(), base_revision=0, by="ana")
    st.add_comment("main", text="note", by="ana", author="operator")
    before = {p.name: p.read_bytes() for p in disk(layout_root).rglob("*") if p.is_file()}
    real = os.replace

    def flaky(src, dst):
        if str(dst).endswith(".comments.json") and f"{os.sep}archive{os.sep}" in str(dst):
            raise OSError("no space")
        return real(src, dst)

    with monkeypatch.context() as m:
        m.setattr(os, "replace", flaky)
        with pytest.raises(S.SaveFailed):
            st.archive("main", base_revision=1, by="bob")
    after = {p.name: p.read_bytes() for p in disk(layout_root).rglob("*") if p.is_file() and "archive" not in p.parts}
    assert after == before and st.exists("main")


# ── comments ─────────────────────────────────────────────────────────────────────────────────


def test_comments_are_numbered_attributed_and_resolvable_once(st):
    st.save(L(), base_revision=0, by="ana")
    a = st.add_comment("main", text="  Is M1 right?  ", by="ana", author="operator", component_id="m1",
                       client={"peer": "127.0.0.1"})
    b = st.add_comment("main", text="agent says hi", by="ext:claude", author="agent")
    assert (a["id"], b["id"]) == ("c1", "c2") and a["text"] == "Is M1 right?" and a["component_id"] == "m1"
    assert (a["author"], a["by"], a["resolved"]) == ("operator", "ana", False) and b["component_id"] is None
    assert [c["id"] for c in st.comments("main")] == ["c1", "c2"]
    assert st.open_comment_count("main") == 2
    done = st.resolve_comment("main", "c1", by="bob", client={"peer": "x"})
    assert done["resolved"] and done["resolved_by"] == "bob" and done["resolved_at"]
    again = st.resolve_comment("main", "c1", by="carl")
    assert again["resolved_by"] == "bob", "resolving twice keeps the first resolver"
    assert [c["id"] for c in st.comments("main", open_only=True)] == ["c2"]
    with pytest.raises(S.NotFound):
        st.resolve_comment("main", "c99", by="bob")
    with pytest.raises(S.NotFound):
        st.comments("nope")


def test_ids_of_comments_are_never_reused(st):
    st.save(L(), base_revision=0, by="ana")
    st.add_comment("main", text="a", by="ana", author="operator")
    st.resolve_comment("main", "c1", by="ana")
    assert st.add_comment("main", text="b", by="ana", author="operator")["id"] == "c2"


def test_a_comment_is_checked(st):
    st.save(L(), base_revision=0, by="ana")
    for kw in (dict(text=""), dict(text="   "), dict(text="x" * 2001), dict(text="ok", component_id="nobody")):
        with pytest.raises(S.Invalid):
            st.add_comment("main", by="ana", author="operator", **kw)
    assert st.add_comment("main", text="x" * 2000, by="ana", author="operator")["id"] == "c1"
    with pytest.raises(S.NotFound):
        st.add_comment("nope", text="x", by="ana", author="operator")


def test_one_author_may_have_only_so_many_unresolved_comments(st):
    st.save(L(), base_revision=0, by="ana")
    for i in range(50):
        st.add_comment("main", text=f"q{i}", by="ext:a", author="agent", max_open_for_author=50)
    with pytest.raises(S.LimitReached) as exc:
        st.add_comment("main", text="one too many", by="ext:a", author="agent", max_open_for_author=50)
    assert exc.value.code == "too_many_open_comments" and exc.value.limit == 50
    st.add_comment("main", text="another agent", by="ext:b", author="agent", max_open_for_author=50)
    st.add_comment("main", text="the operator is not limited", by="ana", author="operator")
    st.resolve_comment("main", "c1", by="ana")
    st.add_comment("main", text="room again", by="ext:a", author="agent", max_open_for_author=50)


def test_a_layout_holds_a_thousand_comments_and_resolved_ones_make_room(st, layout_root):
    st.save(L(), base_revision=0, by="ana")
    path = disk(layout_root, "main.comments.json")
    rows = [{"id": f"c{i}", "author": "operator", "by": "ana", "text": "t", "component_id": None,
             "created_at": "2026-01-01T00:00:00+00:00", "resolved": False, "resolved_by": None,
             "resolved_at": None, "client": None} for i in range(1, 1001)]
    path.write_text(json.dumps({"version": 1, "next": 1001, "comments": rows}), encoding="utf-8")
    with pytest.raises(S.LimitReached) as exc:
        st.add_comment("main", text="full", by="ana", author="operator")
    assert exc.value.code == "layout_comments_full" and len(st.comments("main")) == 1000
    st.resolve_comment("main", "c7", by="ana")
    st.resolve_comment("main", "c3", by="ana")
    new = st.add_comment("main", text="makes room", by="ana", author="operator")
    kept = st.comments("main")
    assert len(kept) == 1000 and new["id"] == "c1001"
    assert "c3" not in {c["id"] for c in kept} and "c7" in {c["id"] for c in kept}, "the oldest resolved one went"
    pruned = disk(layout_root, "archive", "main.comments-pruned.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["id"] for x in pruned] == ["c3"], "and it was kept in the archive"


def test_public_comments_do_not_carry_what_the_server_saw_of_the_writer(st):
    st.save(L(), base_revision=0, by="ana")
    c = st.add_comment("main", text="x", by="ana", author="operator", client={"peer": "203.0.113.7", "user_agent": "u"})
    assert "client" in c
    pub = S.public_comment(c)
    assert "client" not in pub and "203.0.113.7" not in json.dumps(pub)
    assert set(pub) == {"id", "author", "by", "text", "component_id", "created_at", "resolved", "resolved_by", "resolved_at"}


def test_a_broken_comments_file_is_reported_and_kept(st, layout_root):
    st.save(L(), base_revision=0, by="ana")
    path = disk(layout_root, "main.comments.json")
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(S.Corrupt):
        st.comments("main")
    with pytest.raises(S.Corrupt):
        st.add_comment("main", text="x", by="ana", author="operator")
    assert path.read_text(encoding="utf-8") == "{broken"
    assert st.list_layouts()[0]["open_comments"] == 0, "the listing still works"


# ── the history is read a line at a time ─────────────────────────────────────────────────────
#
# A history line is a whole layout (up to a megabyte) and the file keeps 200 of them: read as a
# whole it is up to 200 megabytes in memory for one GET, and again at every save (the trim).


def write_history(layout_root: Path, lid: str, records: list, pad: int = 0, raw_tail: bytes = b"") -> Path:
    """A hand-made history file: one record per line (*pad* bytes of filler on each, outside the
    ``layout`` so that it stays a valid layout), then *raw_tail* verbatim."""
    path = disk(layout_root, "history", f"{lid}.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        for rev, title, by in records:
            lay = L(title=title).model_copy(update={"revision": rev, "updated_by": by}).to_dict()
            rec = {"revision": rev, "saved_at": f"2026-01-01T00:00:{rev % 60:02d}+00:00", "by": by, "client": None,
                   "layout": lay}
            if pad:
                rec["pad"] = "x" * pad
            fh.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        fh.write(raw_tail)
    return path


def peak_bytes(fn) -> int:
    import tracemalloc

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_reading_the_history_holds_one_line_not_the_file(st, layout_root):
    st.save(L(), base_revision=0, by="ana")
    path = write_history(layout_root, "main", [(i, f"v{i}", "ana") for i in range(1, 41)], pad=300_000)
    assert path.stat().st_size > 12_000_000
    assert peak_bytes(lambda: st.history("main", 50)) < 6_000_000          # (a line and its parse: about 2 MB; the file is 12)
    assert peak_bytes(lambda: st.revision("main", 40)) < 6_000_000
    assert peak_bytes(lambda: st.revision("main", 1)) < 6_000_000
    rows = st.history("main", 50)
    assert [r["revision"] for r in rows] == list(range(40, 0, -1)) and rows[0]["title"] == "v40"


def test_a_save_trims_the_history_without_reading_it_whole(st, layout_root):
    st.save(L(), base_revision=0, by="ana")
    path = write_history(layout_root, "main", [(i, f"v{i}", "ana") for i in range(1, 231)], pad=60_000)
    assert path.stat().st_size > 13_000_000 and len(path.read_bytes().splitlines()) == 230
    peak = peak_bytes(lambda: S.LayoutStore._trim_history(path))
    assert peak < 3_000_000, peak
    kept = [json.loads(ln)["revision"] for ln in path.read_bytes().splitlines()]
    assert kept == list(range(31, 231))                      # the newest 200, in order, nothing else
    assert not [p for p in path.parent.iterdir() if ".tmp-" in p.name], "no temp file left behind"
    path2 = write_history(layout_root, "other", [(i, "t", "ana") for i in range(1, 201)])
    before = path2.read_bytes()
    S.LayoutStore._trim_history(path2)
    assert path2.read_bytes() == before, "a history of exactly 200 is left alone"


def test_a_revision_is_found_from_the_end_and_the_reading_stops_there(st, layout_root, monkeypatch):
    st.save(L(), base_revision=0, by="ana")
    write_history(layout_root, "main", [(i, f"v{i}", "ana") for i in range(1, 61)])
    parsed: list[int] = []
    real = json.loads
    with monkeypatch.context() as m:
        m.setattr(S.json, "loads", lambda *a, **k: (parsed.append(1), real(*a, **k))[1])
        assert st.revision("main", 60).title == "v60" and len(parsed) == 1
        parsed.clear()
        assert st.revision("main", 55).title == "v55" and len(parsed) == 6
        parsed.clear()
        assert [r["revision"] for r in st.history("main", 3)] == [60, 59, 58] and len(parsed) == 3
        parsed.clear()
        assert st.history("main", 0) == [] and parsed == []


def test_a_revision_older_than_the_newest_200_is_not_found_even_if_the_file_still_has_it(st, layout_root, monkeypatch):
    """A failed trim leaves the file longer than 200 lines; readers still cut at the newest 200, and
    do not read the rest to find that out."""
    st.save(L(), base_revision=0, by="ana")
    write_history(layout_root, "main", [(i, f"v{i}", "ana") for i in range(1, 251)])
    parsed: list[int] = []
    real = json.loads
    with monkeypatch.context() as m:
        m.setattr(S.json, "loads", lambda *a, **k: (parsed.append(1), real(*a, **k))[1])
        with pytest.raises(S.NotFound):
            st.revision("main", 50)
        assert len(parsed) == 200
        assert st.revision("main", 51).title == "v51"
    assert len(st.history("main", 500)) == 200 and st.history("main", 500)[-1]["revision"] == 51


def test_the_last_line_written_for_a_revision_wins_and_lines_that_are_not_records_are_skipped(st, layout_root):
    """After a crash between the history line and the layout file, the next save writes the same
    revision again; the later line is the truth."""
    st.save(L(), base_revision=0, by="ana")
    write_history(layout_root, "main", [(1, "first", "ana"), (2, "stale", "ana"), (2, "truth", "bob")],
                  raw_tail=b"not json at all\n{}\n[]\n\n   \n" + json.dumps({"revision": "x"}).encode() + b"\n")
    rows = st.history("main", 10)
    assert [(r["revision"], r["title"], r["updated_by"]) for r in rows] == [(2, "truth", "bob"), (1, "first", "ana")]
    assert st.revision("main", 2).title == "truth" and st.revision("main", 1).title == "first"
    lay, saved_at, saved_by = st.revision_record("main", 2)
    assert (lay.title, saved_by) == ("truth", "bob") and saved_at.startswith("2026-01-01T00:00:02")


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n", b"\n\n\n"])
def test_the_reader_handles_any_ending_of_the_file_and_lines_longer_than_its_block(st, layout_root, ending):
    st.save(L(), base_revision=0, by="ana")
    path = write_history(layout_root, "main", [(1, "a", "ana"), (2, "b", "ana")], pad=700_000)
    body = path.read_bytes().rstrip(b"\n")                        # (a line is longer than the 256 KB block)
    if ending == b"\r\n":
        body = body.replace(b"\n", b"\r\n")
    path.write_bytes(body + ending)
    assert [r["revision"] for r in st.history("main", 5)] == [2, 1]
    assert st.revision("main", 1).title == "a"


def test_the_reader_is_a_line_reader_whatever_the_block_size(tmp_path):
    path = tmp_path / "f.bin"
    for content in (b"", b"\n", b"a", b"a\n", b"a\nb", b"a\nb\n", b"\n\na\n\nb\n\n", b"x" * 1000 + b"\n" + b"y" * 3000 + b"\nz"):
        path.write_bytes(content)
        for block in (1, 2, 3, 7, 4096, 1 << 18):
            got = list(S._lines_backwards(path, block))
            assert got == content.split(b"\n")[::-1], (content[:20], block, got[:3])


def test_a_read_that_stops_early_does_not_keep_the_file_open(st, layout_root):
    """On Windows an open handle would block the replace that the next save (or an archive) does."""
    st.save(L(), base_revision=0, by="ana")
    path = write_history(layout_root, "main", [(i, f"v{i}", "ana") for i in range(1, 30)])
    assert st.revision("main", 29).title == "v29"
    assert len(st.history("main", 2)) == 2
    with pytest.raises(S.NotFound):
        st.revision("main", 999)
    os.replace(path, path.with_suffix(".moved"))                 # fails with PermissionError if a handle is still open
    assert not path.exists()


def test_the_trim_leaves_the_history_as_it_was_when_it_cannot_write(st, layout_root, monkeypatch):
    st.save(L(), base_revision=0, by="ana")
    path = write_history(layout_root, "main", [(i, f"v{i}", "ana") for i in range(1, 231)])
    before = path.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(S, "atomic_write", boom)
        S.LayoutStore._trim_history(path)                        # best effort: it only logs
    assert path.read_bytes() == before
    assert len(st.history("main", 500)) == 200, "and the readers still cut at 200"
