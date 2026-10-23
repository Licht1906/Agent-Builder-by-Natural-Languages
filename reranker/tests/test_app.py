"""Tests for the rerank service's ranking and wiring logic (RAG-03).

`app.py` never imports `torch` / `sentence_transformers` at module scope —
both only happen inside `load_scorer`, which nothing here calls. Every test
instead supplies a fake `scorer_factory` to `create_app`, so this file runs
with none of that (large, GPU-relevant) install present. See app.py's
module docstring for why the seam is shaped this way.
"""

from collections.abc import Sequence

from app import RerankResponse, Scorer, create_app, rank
from fastapi.testclient import TestClient


def _fake_scorer(pairs: Sequence[tuple[str, str]]) -> list[float]:
    """A document scores higher the more it repeats a term shared with the query."""
    return [float(len(set(query.split()) & set(doc.split()))) for query, doc in pairs]


def _fake_factory() -> tuple[Scorer, str]:
    return _fake_scorer, "cpu"


def test_rank_orders_by_score_descending() -> None:
    scorer = lambda pairs: [0.1, 0.9, 0.5]  # noqa: E731
    result = rank("q", ["a", "b", "c"], scorer)

    assert isinstance(result, RerankResponse)
    assert result.scores == [0.1, 0.9, 0.5]
    assert result.ranked_indices == [1, 2, 0]


def test_rank_of_no_documents_returns_empty_without_calling_scorer() -> None:
    def _fail(pairs: Sequence[tuple[str, str]]) -> list[float]:
        raise AssertionError("must not score an empty document list")

    result = rank("q", [], _fail)

    assert result.scores == []
    assert result.ranked_indices == []


def test_health_reports_not_loaded_before_startup() -> None:
    app = create_app(scorer_factory=_fake_factory)
    client = TestClient(app)  # no `with` — lifespan has not run yet

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["model_loaded"] is False


def test_health_reports_loaded_after_startup() -> None:
    app = create_app(scorer_factory=_fake_factory)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["model_loaded"] is True
    assert body["device"] == "cpu"


def test_rerank_endpoint_returns_ranked_indices() -> None:
    app = create_app(scorer_factory=_fake_factory)

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={
                "query": "nghỉ việc thai sản",
                "documents": [
                    "quy định về giờ làm việc",
                    "công ty không được cho nghỉ việc khi thai sản",
                    "chính sách nghỉ phép năm",
                ],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body["scores"]) == 3
    assert body["ranked_indices"][0] == 1  # shares the most terms with the query


def test_rerank_endpoint_returns_503_when_model_not_loaded() -> None:
    app = create_app(scorer_factory=_fake_factory)
    client = TestClient(app)  # lifespan never ran

    response = client.post("/rerank", json={"query": "q", "documents": ["a"]})

    assert response.status_code == 503
