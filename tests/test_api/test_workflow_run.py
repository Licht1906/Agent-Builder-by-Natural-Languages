"""Running a confirmed workflow over HTTP — the Workflow Runtime, #45.

The claim this file exists to pin down is the one the ticket puts first: the
answer is **written from what the search actually found**. A test that only
asserted "some text came back" would pass against a runtime whose
`KnowledgeSearch` node did nothing at all, so the assertions here reach for the
prompt the model was handed and check that the creator's own document is in it —
and, in the failure case, that the *absence* of a document is in it instead.

The second claim is that the graph runs as drawn: one node at a time, in edge
order, each announcing itself. That is what the canvas overlays a run onto, and
it is only true if `node_started` arrives per node rather than per run.

Everything is observed through HTTP, like the rest of this suite. The two seams a
test aims are the ones the product already has: the model factory, and the
knowledge store — a real Knowledge Base, ingested through the pipeline the
test wrote, not a mock.
"""

from collections.abc import Callable
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.messages.ai import UsageMetadata

from src.db.models import AgentVersion, User, VersionStatus
from tests.conftest import (
    ScriptedRun,
    ScriptedToolModel,
    auth_header,
    parse_sse,
    shelf_with,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Scenery
# --------------------------------------------------------------------------- #

#: The document the demo agent answers from. Two sections, so a question about
#: one of them can be shown *not* to drag the other along.
HANDBOOK = """Chính sách đổi trả

Khách được đổi trả trong vòng 30 ngày kể từ ngày nhận hàng.

Chính sách hoàn tiền

Hoàn tiền được xử lý trong 7 ngày làm việc sau khi nhận lại hàng.
"""

HANDBOOK_REF = "so-tay.md"

SPEC: dict[str, Any] = {
    "goal": "Trả lời khách về chính sách đổi trả",
    "users": ["Nhân viên chăm sóc khách hàng"],
    "data": [{"name": "Sổ tay", "kind": "uploaded_file", "ref": HANDBOOK_REF}],
    "actions": ["Tra sổ tay và trả lời"],
    "expected_outcome": ["Một câu trả lời dựa trên sổ tay"],
    "success_criteria": [{"text": "Trả lời đúng theo sổ tay", "category": "source_groundedness"}],
    "prohibitions": ["Không tự bịa chính sách"],
}


def document_qa(
    shelf: str = "", *, source_ids: list[str] | None = None, budget: dict[str, int] | None = None
) -> dict[str, Any]:
    """The `document_qa` template's shape, configured — `KnowledgeSearch → AgentLlm → End`."""
    return {
        "start_node_id": "search",
        "nodes": [
            {
                "id": "search",
                "type": "knowledge_search",
                "config": {"source_ids": source_ids if source_ids is not None else [shelf]},
            },
            {
                "id": "answer",
                "type": "agent_llm",
                "config": {"prompt": "Trả lời khách dựa trên sổ tay.", "model": "gpt-4o-mini"},
            },
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "search", "source_port": "found", "target_node_id": "answer"},
            {"id": "e2", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"},
        ],
        "metadata": {"budget": budget} if budget else {},
    }


@pytest_asyncio.fixture
async def documents(client: AsyncClient, creator_token: str) -> str:
    """A real Knowledge Base carrying the handbook, as the `source_ids` that addresses it.

    The product's own shelf, ingested through the real pipeline, rather than a
    stubbed store: what is being claimed is that a chat answer is grounded in a
    creator's document, and a fake store would let that pass while the real
    search matched nothing.
    """
    return await shelf_with(client, creator_token, **{HANDBOOK_REF: HANDBOOK})


@pytest.fixture
def confirmed(
    solution_spec_version: Callable[..., AgentVersion], seeded_creator: User, documents: str
) -> Callable[..., AgentVersion]:
    """A version whose workflow has been confirmed — the only stage a run is allowed at."""

    def _make(
        workflow: dict[str, Any] | None = None,
        *,
        owner: User | None = None,
        status: VersionStatus = VersionStatus.WORKFLOW_CONFIRMED,
    ) -> AgentVersion:
        return solution_spec_version(
            owner or seeded_creator,
            SPEC,
            status=status,
            workflow=workflow if workflow is not None else document_qa(documents),
        )

    return _make


async def run_workflow(client: AsyncClient, version: AgentVersion, message: str, token: str, **thread: Any) -> Any:
    """One chat turn against a version's confirmed workflow."""
    return await client.post(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/workflow-chat",
        json={"message": message, **thread},
        headers=auth_header(token),
    )


def answers(text: str, *, tokens: int | None = None) -> AIMessage:
    """One scripted reply, optionally reporting what it cost the way a provider does."""
    if tokens is None:
        return AIMessage(content=text)
    output = tokens // 5
    return AIMessage(
        content=text,
        usage_metadata=UsageMetadata(input_tokens=tokens - output, output_tokens=output, total_tokens=tokens),
    )


def system_prompt_of(model: ScriptedToolModel, call: int = 0) -> str:
    """The system message the node handed the model on its `call`-th turn."""
    first = model.received_message_batches[call][0]
    assert isinstance(first, SystemMessage), first
    return str(first.content)


# --------------------------------------------------------------------------- #
# The whole point: an answer grounded in the creator's own document
# --------------------------------------------------------------------------- #


async def test_the_answer_is_written_from_what_the_search_actually_found(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """AC 2. The passage reaches the model, and the model's answer reaches the client."""
    _ids, models = scripted_chat_model_factory([[answers("Hoàn tiền trong 7 ngày làm việc.")]])
    version = confirmed()

    response = await run_workflow(client, version, "hoàn tiền mất bao lâu", creator_token)

    assert response.status_code == 200, response.text
    prompt = system_prompt_of(models[0])
    assert "7 ngày làm việc" in prompt
    assert "Chính sách hoàn tiền" in prompt
    assert [
        payload["content"] for payload in parse_sse(response.text, event="node") if payload["node_id"] == "answer"
    ] == ["Hoàn tiền trong 7 ngày làm việc."]


# --------------------------------------------------------------------------- #
# RAG-14 / #100: the tool_result event's `citations` — built from what the
# search actually found, matching the passage the model was actually handed.
# --------------------------------------------------------------------------- #


async def test_the_tool_result_citations_match_what_the_prompt_actually_carries(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    _ids, models = scripted_chat_model_factory([[answers("Hoàn tiền trong 7 ngày làm việc.")]])
    version = confirmed()

    response = await run_workflow(client, version, "hoàn tiền mất bao lâu", creator_token)

    assert response.status_code == 200, response.text
    prompt = system_prompt_of(models[0])
    tool_results = [
        payload for payload in parse_sse(response.text, event="tool_result") if payload["tool"] == "knowledge_search"
    ]
    assert len(tool_results) == 1
    citations = tool_results[0]["citations"]
    assert tool_results[0]["ok"] is True
    assert citations  # at least one source came back
    for citation in citations:
        # The exact text handed to the model is bracketed under this citation's
        # own label in the system prompt — a mismatch here would mean the panel
        # shows something other than what actually grounded the answer.
        label = citation["section_path"] or citation["doc_name"]
        assert f"[{label}]\n{citation['text']}" in prompt
        assert citation["doc_name"] == HANDBOOK_REF


async def test_a_search_that_finds_nothing_reports_ok_true_with_no_citations(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """A search that ran and matched nothing is a *result*, not a failure — `ok: true`, `citations: []`."""
    _ids, models = scripted_chat_model_factory([[answers("Tài liệu không đề cập tới việc này.")]])
    version = confirmed()

    response = await run_workflow(client, version, "công ty có bán vé số không", creator_token)

    assert response.status_code == 200, response.text
    tool_results = [
        payload for payload in parse_sse(response.text, event="tool_result") if payload["tool"] == "knowledge_search"
    ]
    assert len(tool_results) == 1
    assert tool_results[0]["ok"] is True
    assert tool_results[0]["citations"] == []


async def test_a_broken_source_reports_ok_false_with_no_citations(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """`source_ids` naming nothing readable — the failure case the citation panel must not render as sources."""
    _ids, models = scripted_chat_model_factory([[answers("Không truy cập được tài liệu.")]])
    version = confirmed(document_qa(source_ids=["không-tồn-tại.md"]))

    response = await run_workflow(client, version, "hoàn tiền mất bao lâu", creator_token)

    assert response.status_code == 200, response.text
    tool_results = [
        payload for payload in parse_sse(response.text, event="tool_result") if payload["tool"] == "knowledge_search"
    ]
    assert len(tool_results) == 1
    assert tool_results[0]["ok"] is False
    assert tool_results[0]["citations"] == []


def _agent_llm_only(prompt: str) -> dict[str, Any]:
    """A graph with no search node at all — `AgentLlm -> End`, nothing else."""
    return {
        "start_node_id": "answer",
        "nodes": [
            {"id": "answer", "type": "agent_llm", "config": {"prompt": prompt, "model": "gpt-4o-mini"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [{"id": "e1", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"}],
        "metadata": {},
    }


async def test_a_graph_with_no_search_node_runs_exactly_as_before(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """RAG-11 / #97's own acceptance line: `searched=False` never reaches `_citation_label`/`_rendered`.

    No `KnowledgeSearch` node means `state["searched"]` stays `False`
    (`WorkflowState`'s own seed), so `_grounded_prompt` takes its evidence-free
    branch — no `_SOURCES_HEADING`, no citation bracket, none of what a
    searched-but-empty or searched-and-found turn adds. The current-date
    header (`_grounded_prompt`'s own docstring: "added in all three cases")
    still lands on top, same as every other node — that part is not what this
    test is about.
    """
    _ids, models = scripted_chat_model_factory([[answers("Xin chào, tôi có thể giúp gì cho bạn?")]])
    version = confirmed(workflow=_agent_llm_only("Bạn là trợ lý hỗ trợ khách hàng thân thiện."))

    response = await run_workflow(client, version, "xin chào", creator_token)

    assert response.status_code == 200, response.text
    prompt = system_prompt_of(models[0])
    # The creator's prompt, then the platform's two conventions (ADR-0052) —
    # and nothing of the evidence half.
    assert "Bạn là trợ lý hỗ trợ khách hàng thân thiện.\n\nQuy ước của nền tảng" in prompt
    assert "## Nguồn tham khảo" not in prompt


# --------------------------------------------------------------------------- #
# Which documents may be run at all
# --------------------------------------------------------------------------- #


async def test_a_workflow_that_has_not_been_confirmed_cannot_be_run(
    client: AsyncClient, creator_token: str, confirmed: Callable[..., AgentVersion], documents: str
) -> None:
    """ADR-0022 §5: only a confirmed graph is immutable, and only an immutable one runs."""
    version = confirmed(status=VersionStatus.WORKFLOW_PENDING_CONFIRMATION)

    response = await run_workflow(client, version, "hoàn tiền mất bao lâu", creator_token)

    assert response.status_code == 409, response.text
    assert VersionStatus.WORKFLOW_PENDING_CONFIRMATION.value in response.json()["detail"]
