"""What a `KnowledgeSearch` node actually finds — see #45, ADR-0043 and ADR-0044 §3.

The claim under test is the one the ticket makes about grounding: the node hands
the model *passages from the creator's own documents*, chosen by the question
that was asked, and says so plainly when it has nothing to hand over. A store
that returned the whole file, or the same passage regardless of the question,
would satisfy "an answer came back" while making groundedness meaningless.

There is one store now. Until the branches were merged there were two —
`QdrantKnowledgeStore` and a pgvector one that read the shelf off disk at search
time — and this file tested both, which is why the claims below used to be
spelled twice. `get_knowledge_store` builds this one and only this one, so they
are spelled once.

Everything here goes through the **real ingest pipeline** (`shelf_with`) rather
than writing chunks into Qdrant by hand: what a search can find is decided as
much by extraction and chunking as by ranking, so a test that skipped them would
pin the ranker while leaving the part that actually loses answers unmeasured.
`tests/conftest.py`'s fakes stand in for the embedding provider and the reranker
service, and nothing else.

**What is not here.** That a passage the question shares no vocabulary with is
still found is the dense half's whole reason for existing, and it cannot be
asserted offline: the suite's embedder returns one constant vector, so there is
no semantic neighbourhood for a fake to be right about. That claim is measured
against real documents and a real provider in `eval/`, which is the honest place
for it.
"""

from collections.abc import Awaitable, Callable, Sequence

import pytest
from httpx import AsyncClient
from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

from src.db.models import User
from src.services.qdrant import DENSE_VECTOR_SIZE
from src.services.reranker import RerankResult
from src.services.secret_store import EncryptedDbSecretStore
from src.workflow.knowledge import KnowledgeSourceError, QdrantKnowledgeStore
from tests.conftest import knowledge_store_for, shelf_with

pytestmark = pytest.mark.asyncio

HANDBOOK = """# Sổ tay nhân sự

## Chính sách đổi trả

Khách được đổi trả trong vòng 30 ngày kể từ ngày nhận hàng, với điều kiện hàng
còn nguyên tem và hoá đơn.

## Chính sách hoàn tiền

Hoàn tiền được xử lý trong 7 ngày làm việc sau khi nhận lại hàng, chuyển vào
tài khoản khách đã đăng ký.

## Chế độ ngày nghỉ hằng năm

Nhân viên chính thức được 12 ngày phép mỗi năm, cộng thêm một ngày cho mỗi năm
thâm niên.
"""


@pytest.fixture
def store(
    db_session: Session, seeded_creator: User, _no_live_ingestion_backends: QdrantClient
) -> QdrantKnowledgeStore:
    """The product's own store over this test's own Qdrant — see `knowledge_store_for`."""
    return knowledge_store_for(db_session, seeded_creator.id, _no_live_ingestion_backends)


@pytest.fixture
def shelf(client: AsyncClient, creator_token: str) -> Callable[..., Awaitable[str]]:
    """`await shelf(**{"ten-tep.md": "nội dung"})` → the `source_ids` string for a shelf carrying them."""

    async def _make(**files: str) -> str:
        return await shelf_with(client, creator_token, **files)

    return _make


async def test_finds_the_passage_that_answers_the_question(
    store: QdrantKnowledgeStore, shelf: Callable[..., Awaitable[str]]
) -> None:
    """The section the question is about comes back, and the one it isn't does not."""
    kb = await shelf(**{"so-tay.md": HANDBOOK})

    passages = store.search("hoàn tiền mất bao lâu", source_refs=[kb])

    assert any("7 ngày làm việc" in passage.text for passage in passages)
    assert not any("còn nguyên tem" in passage.text for passage in passages)


async def test_a_passage_carries_the_section_it_came_from(
    store: QdrantKnowledgeStore, shelf: Callable[..., Awaitable[str]]
) -> None:
    """The Section Path is what a citation names (RAG-11), so a person auditing
    the answer can tell which policy it was read out of."""
    kb = await shelf(**{"so-tay.md": HANDBOOK})

    passages = store.search("hoàn tiền mất bao lâu", source_refs=[kb])

    assert "Chính sách hoàn tiền" in (passages[0].section_path or "")
    assert passages[0].doc_name == "so-tay.md"


async def test_says_nothing_rather_than_something_irrelevant(
    store: QdrantKnowledgeStore, shelf: Callable[..., Awaitable[str]]
) -> None:
    """No match is a result. It is what lets the answering node decline instead
    of grounding an invented answer in whatever scored least badly."""
    kb = await shelf(**{"so-tay.md": HANDBOOK})

    assert store.search("mật khẩu quản trị", source_refs=[kb]) == ()


async def test_a_long_document_keeps_the_sentence_that_answers_the_question(
    store: QdrantKnowledgeStore, shelf: Callable[..., Awaitable[str]]
) -> None:
    """The regression that made the retrieval rewrite urgent.

    A real regulation PDF put "không được vượt quá 5 học kỳ chính" 1253
    characters into a 2670-character block; the store of the day cut every block
    at 1200 and appended an ellipsis, so the agent answered "the regulation does
    not say" about a rule written down in the document it was citing. Nothing may
    be dropped for sitting far from the start of its section.
    """
    filler = " ".join(f"Điều khoản phụ số {number} không liên quan tới câu hỏi." for number in range(60))
    kb = await shelf(
        **{
            "quy-che.md": (
                "## Thời gian học tập\n\n"
                f"{filler} "
                "Thời gian chậm tiến độ không được vượt quá 5 học kỳ chính đối với chương trình cử nhân."
            )
        }
    )

    passages = store.search("thời gian chậm tiến độ tối đa của cử nhân", source_refs=[kb])

    assert any("5 học kỳ chính" in passage.text for passage in passages)


async def test_one_section_cannot_take_every_slot(
    store: QdrantKnowledgeStore, shelf: Callable[..., Awaitable[str]]
) -> None:
    """Measured on a real 34-page regulation: the top slots were two sections
    shown twice each, and the clause that answered the question sat just below
    the cut. Two chunks of one clause are two views of one rule; a third is the
    next *section* going unread.

    The cap is what is asserted, and only the cap. That the slots freed up go
    to *more* sections is the thing worth having, but it depends on which
    candidates the prefetch surfaced — and this suite's embedder returns one
    constant vector, so candidate order here is an artefact of the fake rather
    than a property of the product. `eval/` measures that against real
    embeddings.
    """
    #: Distinct wording per clause, so what keeps a section from taking every
    #: slot is the cap and not two chunks reading as the same text.
    topics = ["đăng ký", "phê duyệt", "thâm niên", "chuyển tiếp", "báo trước", "hồ sơ", "thanh toán", "tồn đọng"]
    sections = "\n\n".join(
        f"Điều {number}. Chế độ nhóm {number}\n\n"
        + "\n\n".join(
            f"Khoản {clause} điều {number} quy định việc {topics[clause % len(topics)]} khi nghỉ phép, "
            f"áp dụng cho nhóm {number}{clause} với thời hạn {number * clause} ngày."
            for clause in range(1, 12)
        )
        for number in range(1, 5)
    )
    kb = await shelf(**{"quy-che.md": sections})

    passages = store.search("nghỉ phép", source_refs=[kb], limit=6)

    returned = [passage.section_path for passage in passages]
    assert returned
    assert all(returned.count(section) <= 2 for section in returned), returned


async def test_a_ref_that_is_not_a_knowledge_base_id_is_refused_by_name(store: QdrantKnowledgeStore) -> None:
    """`source_ids` holds Knowledge Base ids (ADR-0043 §1). A ref that is not one
    cannot be searched, and the creator reading the failure needs to know which
    entry in their own list to fix — so the message names it."""
    with pytest.raises(KnowledgeSourceError, match="so-tay.md"):
        store.search("hoàn tiền", source_refs=["so-tay.md"])


async def test_a_shelf_that_is_not_the_owners_is_refused_rather_than_skipped(
    store: QdrantKnowledgeStore, client: AsyncClient, second_creator_token: str
) -> None:
    """A source silently dropped is an answer that looks grounded in every shelf
    the creator attached while resting on fewer of them."""
    theirs = await shelf_with(client, second_creator_token, name="Tủ của người khác", **{"luong.md": "bang luong"})

    with pytest.raises(KnowledgeSourceError, match=theirs):
        store.search("lương", source_refs=[theirs])


async def test_one_section_cannot_take_every_slot_even_when_the_reranker_is_down(
    db_session: Session,
    seeded_creator: User,
    _no_live_ingestion_backends: QdrantClient,
    shelf: Callable[..., Awaitable[str]],
) -> None:
    """The cap is about the *answer*, so losing the reranker must not lose it too.

    A dead reranker is the likeliest production state of this service — it
    degrades rather than failing (`src.services.reranker`), which is exactly
    why the degraded result has to stay worth reading. Dropping the threshold
    there is deliberate and reasoned (an RRF rank score is not comparable to
    the cross-encoder score the threshold is calibrated against); dropping the
    per-section cap with it was not, and it lets one Điều take all eight slots
    on the one path nobody is watching.
    """

    def _dead_reranker(_query: str, documents: Sequence[str]) -> RerankResult:
        return RerankResult(ranked_indices=list(range(len(documents))), scores=None, reranked=False)

    store = QdrantKnowledgeStore(
        db=db_session,
        owner_id=seeded_creator.id,
        qdrant_client=_no_live_ingestion_backends,
        embedding_client=lambda texts: [[0.1] * DENSE_VECTOR_SIZE for _ in texts],
        reranker_client=_dead_reranker,
        secret_store=EncryptedDbSecretStore(db=db_session),
    )
    topics = ["đăng ký", "phê duyệt", "thâm niên", "chuyển tiếp", "báo trước", "hồ sơ"]
    sections = "\n\n".join(
        f"Điều {number}. Chế độ nhóm {number}\n\n"
        + "\n\n".join(
            f"Khoản {clause} điều {number} quy định việc {topics[clause % len(topics)]} khi nghỉ phép, "
            f"áp dụng cho nhóm {number}{clause} với thời hạn {number * clause} ngày."
            for clause in range(1, 12)
        )
        for number in range(1, 5)
    )
    kb = await shelf(**{"quy-che.md": sections})

    passages = store.search("nghỉ phép", source_refs=[kb], limit=6)

    returned = [passage.section_path for passage in passages]
    assert returned
    assert all(returned.count(section) <= 2 for section in returned), returned
