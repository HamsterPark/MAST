"""The observation gate (mast.vista.gate): token minting, consumption and the refusal order."""
from __future__ import annotations

import threading

from mast.vista.gate import (ACT_IN_FLIGHT, CHECKPOINT_REQUESTED, OBSERVE_FIRST, SAME_CODE_CELL,
                             SESSION_CLOSED, ALPHABET, Grant, ObservationGate, Refusal,
                             new_token, normalise_token)


def test_tokens_use_the_unambiguous_alphabet_and_normalise():
    for _ in range(200):
        t = new_token()
        assert len(t) == 9 and t[4] == "-"
        assert all(ch in ALPHABET for ch in t.replace("-", ""))
        assert normalise_token(t.lower().replace("-", " ")) == t
    assert normalise_token("ABCD-EFG") == ""             # too short
    assert normalise_token("OOOO-0000") == ""            # look-alikes are not token characters
    assert normalise_token(None) == ""


def test_an_act_consumes_the_live_token_once():
    g = ObservationGate()
    tok = g.mint("s1", "o1")
    grant = g.acquire(session="s1", after=tok)
    assert isinstance(grant, Grant)
    assert g.status()["token_live"] is False
    again = g.acquire(session="s1", after=tok)
    assert isinstance(again, Refusal) and again.code == ACT_IN_FLIGHT
    nxt = g.complete(grant.ticket, "s1", "o2")
    assert nxt != tok and g.is_live(nxt)
    stale = g.acquire(session="s1", after=tok)
    assert isinstance(stale, Refusal) and stale.code == OBSERVE_FIRST and stale.recover


def test_a_token_belongs_to_the_session_it_was_delivered_to():
    g = ObservationGate()
    tok = g.mint("s1", "o1")
    r = g.acquire(session="s2", after=tok)
    assert isinstance(r, Refusal) and r.code == OBSERVE_FIRST
    assert g.is_live(tok)                                 # a refusal changes nothing


def test_refusal_order_closed_inflight_cell_checkpoint_token():
    g = ObservationGate()
    tok = g.mint("s1", "o1")
    assert g.acquire(session="s1", after=tok, closed=True).code == SESSION_CLOSED
    first = g.acquire(session="s1", after=tok, item_id="cell-A")
    assert isinstance(first, Grant)
    assert g.acquire(session="s1", after="x", item_id="cell-A").code == ACT_IN_FLIGHT
    tok2 = g.complete(first.ticket, "s1", "o2")
    assert g.acquire(session="s1", after=tok2, item_id="cell-A").code == SAME_CODE_CELL
    assert g.acquire(session="s1", after=tok2, checkpoint=True).code == CHECKPOINT_REQUESTED
    assert g.acquire(session="s1", after="nope").code == OBSERVE_FIRST
    assert isinstance(g.acquire(session="s1", after=tok2, item_id="cell-B"), Grant)


def test_an_outside_frame_invalidates_the_token():
    g = ObservationGate()
    tok = g.mint("s1", "o1")
    assert g.invalidate("external frame t0000.s0") is True
    r = g.acquire(session="s1", after=tok)
    assert isinstance(r, Refusal) and r.code == OBSERVE_FIRST
    assert g.status()["last_invalidation"]["reason"].startswith("external frame")


def test_abort_gives_the_token_back_when_the_act_never_started():
    g = ObservationGate()
    tok = g.mint("s1", "o1")
    grant = g.acquire(session="s1", after=tok, item_id="cell-A")
    g.abort(grant)
    assert g.in_flight() is None and g.is_live(tok)
    assert isinstance(g.acquire(session="s1", after=tok, item_id="cell-A"), Grant)


def test_release_without_an_observation_leaves_no_token():
    g = ObservationGate()
    grant = g.acquire(session="s1", after=g.mint("s1", "o1"))
    g.release(grant.ticket)
    assert g.in_flight() is None and g.status()["token_live"] is False


def test_status_never_contains_the_token():
    g = ObservationGate()
    tok = g.mint("s1", "o1")
    assert tok not in str(g.status())


def test_only_one_of_many_concurrent_acts_with_the_same_token_is_accepted():
    g = ObservationGate()
    tok = g.mint("s1", "o1")
    results: list = []
    barrier = threading.Barrier(16)

    def go(i):
        barrier.wait()
        results.append(g.acquire(session="s1", after=tok, item_id=f"cell-{i}"))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(isinstance(r, Grant) for r in results) == 1


def test_item_memory_is_bounded():
    g = ObservationGate(item_memory=4)
    for i in range(10):
        grant = g.acquire(session="s1", after=g.mint("s1", f"o{i}"), item_id=f"cell-{i}")
        g.complete(grant.ticket, "s1", f"p{i}")
    assert len(g._items) == 4
