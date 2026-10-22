"""What happens as the token allowance runs out — see ADR-0045 §8.

No provider will say how much you have spent with it: Voyage publishes a
per-model grant and no endpoint to query it, OpenAI simply bills. So the
platform counts for itself. The claim under test is that the count is kept
honestly and *acted on*: the next model is used when one runs out, keyword-only
search when they all do, and never a request that quietly spends past the
ceiling.

Nothing here touches the network. `record` is the same function the real
embedders call; what the tests supply is the number, which is the only thing
the provider contributes.
"""

import pytest
from sqlalchemy.orm import Session

from src.config import get_settings
from src.db.models import EmbeddingUsage
from src.workflow.embedding_budget import (
    FREE_TOKEN_ALLOWANCE,
    get_embedder,
    record,
    spent,
    usable_model,
    usage_report,
)
from src.workflow.embeddings import EMBEDDING_DIMENSIONS

ALLOWANCE = 1_000


@pytest.fixture(autouse=True)
def _empty_ledger(db_session: Session) -> None:
    """Start every test with nothing spent.

    The ledger is deliberately cumulative *and* deliberately never reset by the
    application — that is what makes it able to answer "how much of the grant is
    gone". So a test that spends has to clear up after the previous one, or the
    suite's own totals become the thing under test.
    """
    db_session.query(EmbeddingUsage).delete()
    db_session.commit()


def test_a_call_is_written_down(db_session: Session) -> None:
    """The ledger is the only place this number can ever come from."""
    record("voyage-4", 120)
    record("voyage-4", 80)

    assert spent(db_session, "voyage-4") == 200


def test_models_are_counted_separately(db_session: Session) -> None:
    """The allowance is per model, so a total that mixed them could not answer
    the one question this ledger exists for."""
    record("voyage-4", 300)
    record("voyage-4-lite", 50)

    assert spent(db_session, "voyage-4") == 300
    assert spent(db_session, "voyage-4-lite") == 50


def test_uses_the_first_model_while_it_has_allowance(db_session: Session) -> None:
    record("voyage-4", ALLOWANCE // 2)

    assert usable_model(db_session, ["voyage-4", "voyage-4-lite"], allowance=ALLOWANCE) == "voyage-4"


def test_moves_to_the_next_model_when_the_first_is_exhausted(db_session: Session) -> None:
    """Configuring two models is how "switch instead of stopping" is expressed.

    Note this re-embeds every library on its next search — vectors from two
    models are not comparable — which is the cost of the switch and the reason
    it is opt-in rather than automatic.
    """
    record("voyage-4", ALLOWANCE)

    assert usable_model(db_session, ["voyage-4", "voyage-4-lite"], allowance=ALLOWANCE) == "voyage-4-lite"


def test_stops_using_embeddings_when_every_model_is_exhausted(db_session: Session) -> None:
    """`None`, not an exception. An exhausted budget is a budget working, and a
    node that started raising would take down agents that answered fine a minute
    earlier — they fall back to keyword ranking instead."""
    record("voyage-4", ALLOWANCE)
    record("voyage-4-lite", ALLOWANCE)

    assert usable_model(db_session, ["voyage-4", "voyage-4-lite"], allowance=ALLOWANCE) is None


def test_one_model_configured_means_stop_rather_than_switch(db_session: Session) -> None:
    """The other half of the same setting: a single-entry list is the "dừng lại"
    behaviour, with no second model to fall through to."""
    record("voyage-4", ALLOWANCE)

    assert usable_model(db_session, ["voyage-4"], allowance=ALLOWANCE) is None


def test_a_zero_allowance_means_no_ceiling(db_session: Session) -> None:
    """What a paid account sets, and the default now that the default provider
    bills rather than grants. The ledger goes on counting — the figure is still
    worth reading — but nothing is ever retired."""
    record("text-embedding-3-small", 10 * FREE_TOKEN_ALLOWANCE)

    assert usable_model(db_session, ["text-embedding-3-small"], allowance=0) == "text-embedding-3-small"


def test_the_report_says_how_much_is_left(db_session: Session) -> None:
    """What the admin screen renders, and what somebody checks before deciding
    whether to add a second model."""
    record("voyage-4", 250)

    report = usage_report(db_session, ["voyage-4"], allowance=ALLOWANCE)

    assert report[0].tokens == 250
    assert report[0].remaining == 750
    assert report[0].used_fraction == pytest.approx(0.25)
    assert not report[0].exhausted


def test_no_model_configured_means_no_embedder(db_session: Session) -> None:
    """The state every test run is in — see `_no_live_embedding_provider`."""
    assert get_embedder(db_session) is None


def test_a_key_and_a_spent_allowance_also_means_no_embedder(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure this whole module exists to prevent: spending past the free
    tier without noticing. With the allowance gone, no request is built at all."""
    settings = get_settings()
    monkeypatch.setattr(settings, "voyage_api_key", "test-key")
    monkeypatch.setattr(settings, "embedding_model", "voyage-4")
    monkeypatch.setattr(settings, "embedding_token_allowance", ALLOWANCE)
    record("voyage-4", ALLOWANCE)

    assert get_embedder(db_session) is None


def test_a_key_within_allowance_builds_an_embedder_that_records(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The embedder handed out is wired to the ledger — otherwise the ceiling
    above is measured against a number nothing updates."""
    settings = get_settings()
    monkeypatch.setattr(settings, "voyage_api_key", "test-key")
    monkeypatch.setattr(settings, "embedding_model", " voyage-4 , voyage-4-lite ")
    monkeypatch.setattr(settings, "embedding_token_allowance", ALLOWANCE)

    embedder = get_embedder(db_session)

    assert embedder is not None
    assert embedder.model == "voyage-4"
    embedder.record_usage(embedder.model, 42)  # type: ignore[attr-defined]
    assert spent(db_session, "voyage-4") == 42


def test_the_model_setting_is_an_ordered_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace and a trailing comma are what a person actually types."""
    settings = get_settings()
    monkeypatch.setattr(settings, "embedding_model", "voyage-4, voyage-4-lite,")

    assert settings.embedding_models == ("voyage-4", "voyage-4-lite")


def test_the_provider_is_read_off_the_model_name(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    """`text-embedding-*` is OpenAI, `voyage-*` is Voyage — one rule, one place.

    A second setting naming the provider could disagree with the model name, and
    that state has no sensible behaviour. Here it cannot be expressed.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "openai-key")
    monkeypatch.setattr(settings, "voyage_api_key", "voyage-key")

    monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-small")
    assert type(get_embedder(db_session)).__name__ == "OpenAIEmbedder"

    monkeypatch.setattr(settings, "embedding_model", "voyage-4")
    assert type(get_embedder(db_session)).__name__ == "VoyageEmbedder"


def test_a_model_whose_provider_has_no_key_is_not_used(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    """Configuring a Voyage model without a Voyage key is not a crash — it is
    the same supported state as configuring nothing, so the app still starts and
    search still answers, from keywords."""
    settings = get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "openai-key")
    monkeypatch.setattr(settings, "voyage_api_key", "")
    monkeypatch.setattr(settings, "embedding_model", "voyage-4")

    assert get_embedder(db_session) is None


def test_skips_a_missing_provider_key_and_uses_the_next_model(
    monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    """A missing key must not block the next configured provider."""
    settings = get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "voyage_api_key", "voyage-key")
    monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-small,voyage-4")

    embedder = get_embedder(db_session)

    assert embedder is not None
    assert embedder.model == "voyage-4"


def test_both_providers_ask_for_the_same_vector_width(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> None:
    """The database column is 1024 wide whoever fills it.

    OpenAI's models are natively 1536 or 3072 and are shortened on request;
    Voyage's are 1024 already. If this ever diverged, the failure would surface
    as a Postgres error about a column, far from the setting that caused it.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "openai_api_key", "openai-key")
    monkeypatch.setattr(settings, "voyage_api_key", "voyage-key")

    monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-small")
    openai_embedder = get_embedder(db_session)
    monkeypatch.setattr(settings, "embedding_model", "voyage-4")
    voyage_embedder = get_embedder(db_session)

    assert openai_embedder is not None and voyage_embedder is not None
    assert openai_embedder.dimensions == voyage_embedder.dimensions == EMBEDDING_DIMENSIONS
