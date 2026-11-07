"""``CognitionContext.recall_scored``: the rows ``recall`` returns, plus how each was found.

``recall`` feeds the internal agents' memory middleware, so it must not change; the external
note search (``GET /notes``) shows the score so an unrelated nearest neighbour can be told apart
(e.g. a global "dream consolidation" note comes back for a query about registration
ambiguity, with nothing to say it was the weakest of the k)."""
from __future__ import annotations

import pytest


def _force_hash_embedder(monkeypatch):
    import mast.memory.embeddings as emb
    from mast.logging.v2.vector_search import HashEmbedder

    monkeypatch.setattr(emb, "make_embedder",
                        lambda prefer="auto": (HashEmbedder(dim=64), 64, "hashtest"))


def _cog(tmp_path):
    from mast.agents._shared.cognition import CognitionContext

    cog = CognitionContext(str(tmp_path / "exp.db"), author="user")
    cog.remember("experiment:E1", "n/reg.md", "registration ambiguity periodic supercell diff",
                 title="reg", kind="insight", experiment_id="E1")
    cog.remember("global", "dreams/c.md", "consolidated workflow protocol baseline library",
                 title="dream", kind="dream")
    cog.remember("experiment:E2", "n/other.md", "registration ambiguity elsewhere",
                 title="other", kind="note", experiment_id="E2")
    return cog


def test_semantic_hits_carry_a_cosine_score_and_rank_like_recall(tmp_path, monkeypatch):
    _force_hash_embedder(monkeypatch)
    cog = _cog(tmp_path)
    scored = cog.recall_scored("registration ambiguity periodic supercell", experiment_id="E1", k=5)
    assert scored and all(how == "semantic" for _r, _s, how in scored)
    scores = [s for _r, s, _h in scored]
    assert all(isinstance(s, float) and -1.0 <= s <= 1.0 for s in scores)
    assert scores == sorted(scores, reverse=True)          # nearest first
    assert scored[0][0]["path"] == "n/reg.md"
    assert {r["namespace"] for r, _s, _h in scored} <= {"experiment:E1", "global"}  # no leak
    # the related note scores clearly above the unrelated global one
    by_path = {r["path"]: s for r, s, _h in scored}
    assert by_path["n/reg.md"] > by_path["dreams/c.md"]


def test_recall_returns_exactly_the_rows_of_recall_scored(tmp_path, monkeypatch):
    _force_hash_embedder(monkeypatch)
    cog = _cog(tmp_path)
    q = "registration ambiguity"
    assert cog.recall(q, experiment_id="E1", k=5) == [
        r for r, _s, _h in cog.recall_scored(q, experiment_id="E1", k=5)]


def test_without_a_vector_index_the_hits_say_substring_and_carry_no_score(tmp_path, monkeypatch):
    from mast.agents._shared.cognition import CognitionContext

    monkeypatch.setattr(CognitionContext, "_ensure_vec", lambda self: None)
    cog = _cog(tmp_path)
    scored = cog.recall_scored("supercell", experiment_id="E1", k=5)
    assert [(r["path"], s, how) for r, s, how in scored] == [("n/reg.md", None, "substring")]
    assert cog.recall("supercell", experiment_id="E1", k=5) == [scored[0][0]]


@pytest.mark.parametrize("backend, distance, expected", [
    ("sqlite_vec", 0.6324555, 0.8),        # L2 on unit vectors: cos 0.8 (measured, sqlite-vec)
    ("numpy", 0.2, 0.8),                   # 1 - cos
    ("other", 0.2, None),                  # unknown metric: no guess
    ("numpy", "nan", None),
])
def test_the_distance_to_similarity_conversion(backend, distance, expected):
    from mast.agents._shared.cognition import _similarity
    from mast.logging.v2.vector_search import NumpyFallbackSearch, SqliteVecSearch

    vec = {"sqlite_vec": SqliteVecSearch.__new__(SqliteVecSearch),
           "numpy": NumpyFallbackSearch.__new__(NumpyFallbackSearch),
           "other": object()}[backend]
    got = _similarity(vec, float(distance) if distance != "nan" else float("nan"))
    assert got == (pytest.approx(expected, abs=1e-4) if expected is not None else None)
