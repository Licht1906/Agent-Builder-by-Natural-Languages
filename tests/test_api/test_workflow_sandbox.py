"""Trialling a confirmed workflow in the sandbox (#46): the transcript, and what it earns.

The claim this file pins down is the one the approval flow rests on, restated for
V2: **the evidence an admin reads is the run that actually happened**. So the
assertions reach past "a transcript exists" and into what is in it — the passage
the search really found, the node ids that point into the graph the creator drew,
and the price of the run — because a transcript that merely existed would pass
against a sandbox that recorded nothing but its own optimism.

The second claim is the gate. `mark_sandbox_passed` is what turns a workflow into
something an admin can be asked to approve, and it must be unreachable except by
finishing a run. Three tests hold that door: a finished run opens it, a run
stopped by its own budget does not, and a version already past the gate is not
dragged backwards through it.

Everything is observed through HTTP like the rest of the suite. The two seams a
test aims are the product's own — the model factory and the knowledge store — and
the store is the product's own, over a Knowledge Base the test ingested,
so "the answer is grounded" stays a checkable claim.
"""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from langchain_core.messages import AIMessage
from langchain_core.messages.ai import UsageMetadata
from sqlalchemy.orm import Session

from src.agents.tools import builtin
from src.config import get_settings
from src.db.models import AgentVersion, SandboxRunStatus, User, VersionStatus
from tests.conftest import ScriptedRun, auth_header, parse_sse, parse_sse_stream, shelf_with

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Scenery — the `document_qa` demo, grounded in a document the test uploaded
# --------------------------------------------------------------------------- #

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
    shelf: str = "", *, source_ids: list[str] | None = None, budget: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The demo graph: `KnowledgeSearch → AgentLlm → End`."""
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


def web_lookup(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """A graph whose first node is the *other* read-only node type — `WebSearch → AgentLlm → End`.

    `config` defaults to empty, which is a `WebSearch` node as it was before
    ADR-0042 gave the type any fields — so most callers keep asserting about the
    unrefined node every existing graph still holds.
    """
    return {
        "start_node_id": "lookup",
        "nodes": [
            {"id": "lookup", "type": "web_search", "config": config or {}},
            {
                "id": "answer",
                "type": "agent_llm",
                "config": {"prompt": "Tóm tắt những gì tìm được.", "model": "gpt-4o-mini"},
            },
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "lookup", "source_port": "done", "target_node_id": "answer"},
            {"id": "e2", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"},
        ],
        "metadata": {},
    }


def web_lookup_behind_an_extractor() -> dict[str, Any]:
    """ADR-0068's shape — `AgentLlm → WebSearch → AgentLlm → End`.

    The first node's whole job is to write the search terms. `web_lookup`'s graph
    is kept beside it unchanged, because the compatibility claim is that a
    `WebSearch` node with nothing in front of it still searches the question.
    """
    return {
        "start_node_id": "extract",
        "nodes": [
            {
                "id": "extract",
                "type": "agent_llm",
                "config": {"prompt": "In ra đúng tên công ty cần tra, trên một dòng.", "model": "gpt-4o-mini"},
            },
            {"id": "lookup", "type": "web_search", "config": {}},
            {
                "id": "answer",
                "type": "agent_llm",
                "config": {"prompt": "Tóm tắt những gì tìm được.", "model": "gpt-4o-mini"},
            },
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "extract", "source_port": "done", "target_node_id": "lookup"},
            {"id": "e2", "source_node_id": "lookup", "source_port": "done", "target_node_id": "answer"},
            {"id": "e3", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"},
        ],
        "metadata": {},
    }


@pytest_asyncio.fixture
async def documents(client: AsyncClient, creator_token: str) -> str:
    """A real Knowledge Base carrying the handbook — see `test_workflow_run`."""
    return await shelf_with(client, creator_token, **{HANDBOOK_REF: HANDBOOK})


@pytest.fixture
def a_configured_search_api(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Make the *real* `web_search` fully live, answering from a stubbed endpoint.

    The same shape as `smtp_that_must_never_be_reached` and the opposite claim.
    Without a key the real implementation refuses early, and a test resting on
    that would prove only that the search API was unconfigured — not that a
    result the search really returned reaches the transcript, which is what AC 1
    asks. With the key set, the only thing between the node and a real HTTP call
    is `ToolSpec.implementation`, and this stubs the wire underneath it rather
    than the tool above it: everything the product runs in a trial — the
    resolver, `invoke_tool`, the arg schema, the response parsing — really runs.
    """
    monkeypatch.setattr(get_settings(), "search_api_key", "test-key")
    # Returned so a test can assert on what the node *asked for*, not only on
    # what it did with the reply: a `query_hint` dropped between config and wire
    # is invisible in a result the stub was going to return regardless.
    sent: list[dict[str, Any]] = []

    def _respond(url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float) -> Any:
        assert json["query"], json
        sent.append(json)
        # The credential travels in the header the provider documents. Asserted
        # here because this stub is the only place the outgoing request is ever
        # seen: send it in the body instead and every test still passes while
        # the live endpoint answers 401.
        assert headers["Authorization"] == "Bearer test-key", headers
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Giá vàng SJC hôm nay",
                        "url": "https://example.com/gia-vang",
                        "content": "SJC niêm yết 92,4 triệu đồng/lượng.",
                    }
                ]
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(builtin.httpx, "post", _respond)
    return sent


@pytest.fixture
def confirmed(
    solution_spec_version: Callable[..., AgentVersion], seeded_creator: User, documents: str
) -> Callable[..., AgentVersion]:
    """A version whose workflow has been confirmed — the stage a V2 trial starts from."""

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


async def trial_workflow(client: AsyncClient, version: AgentVersion, message: str, token: str, **thread: Any) -> Any:
    """One sandbox run of a version's confirmed workflow."""
    return await client.post(
        f"/api/v1/agents/{version.agent_id}/versions/{version.id}/workflow-sandbox-runs",
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


async def stored_run(client: AsyncClient, version: AgentVersion, run_id: int, token: str) -> Any:
    """The saved transcript, read back through the endpoint an approval dossier uses."""
    return await client.get(
        f"/api/v1/agents/{version.agent_id}/sandbox-runs/{run_id}",
        headers=auth_header(token),
    )


def sandbox_record(response: Any) -> dict[str, Any]:
    """The closing `sandbox_run` event — where the evidence landed and what it changed."""
    records = parse_sse(response.text, event="sandbox_run")
    assert len(records) == 1, response.text
    return records[0]


# --------------------------------------------------------------------------- #
# AC 1 & 2 — a real read-only result, on a flat per-node transcript
# --------------------------------------------------------------------------- #


async def test_the_transcript_carries_the_passage_the_search_really_found(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """AC 1. `KnowledgeSearch` is read-only, so a trial run really searches the creator's file."""
    scripted_chat_model_factory([[answers("Hoàn tiền trong 7 ngày làm việc.")]])
    version = confirmed()

    response = await trial_workflow(client, version, "hoàn tiền mất bao lâu", creator_token)

    assert response.status_code == 200, response.text
    saved = await stored_run(client, version, sandbox_record(response)["run_id"], creator_token)
    assert saved.status_code == 200, saved.text
    results = [line for line in saved.json()["transcript"] if line["kind"] == "tool_result"]
    assert [line["tool"] for line in results] == ["knowledge_search"]
    assert "7 ngày làm việc" in results[0]["content"]
    # Real, not mocked: a read-only node runs its own implementation in both modes.
    assert results[0]["simulated"] is False
    assert results[0]["ok"] is True


async def test_the_transcript_is_a_flat_list_naming_the_nodes_of_the_graph(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """AC 2. One ordered list, each line naming the node that produced it — never a `step_id`."""
    scripted_chat_model_factory([[answers("Hoàn tiền trong 7 ngày làm việc.")]])
    version = confirmed()

    response = await trial_workflow(client, version, "hoàn tiền mất bao lâu", creator_token)

    saved = await stored_run(client, version, sandbox_record(response)["run_id"], creator_token)
    transcript = saved.json()["transcript"]
    assert [(line["kind"], line["node_id"]) for line in transcript] == [
        ("tool", "search"),
        ("tool_result", "search"),
        ("node", "search"),
        ("node", "answer"),
        ("node", "finish"),
    ]
    # The ids point into `WorkflowJSON.nodes`, so nothing may arrive under V1's name.
    assert {line["step_id"] for line in transcript} == {None}
    assert [line["content"] for line in transcript if line["node_id"] == "answer"] == [
        "Hoàn tiền trong 7 ngày làm việc."
    ]


async def test_a_web_search_nodes_real_results_reach_the_transcript(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    a_configured_search_api: list[dict[str, Any]],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """AC 1's other half: the *result*, not merely the attempt, is in the evidence.

    A trial run reaches the real `web_search` — nothing is swapped for a mock —
    so what the search returned is what an admin reads back, and what the
    answering node was given to write from.
    """
    scripted_chat_model_factory([[answers("Giá vàng SJC đang được niêm yết.")]])
    version = confirmed(web_lookup())

    response = await trial_workflow(client, version, "giá vàng hôm nay", creator_token)

    assert response.status_code == 200, response.text
    saved = await stored_run(client, version, sandbox_record(response)["run_id"], creator_token)
    results = [line for line in saved.json()["transcript"] if line["kind"] == "tool_result"]
    assert [line["tool"] for line in results] == ["web_search"]
    assert results[0]["ok"] is True
    assert results[0]["simulated"] is False
    assert "Giá vàng SJC hôm nay" in results[0]["content"]
    # Not just the headline: the snippet is what the answering node had to write
    # from, so a transcript holding only the link would be evidence of a search
    # that grounded nothing.
    assert "SJC niêm yết 92,4 triệu đồng/lượng." in results[0]["content"]


async def test_a_web_search_nodes_own_configuration_narrows_the_search_it_sends(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    a_configured_search_api: list[dict[str, Any]],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """ADR-0042. The creator's refinement reaches the provider, and the turn's question still leads.

    Asserted on the outgoing payload because that is the only place the claim is
    visible: the stub answers the same way whatever it is asked, so a
    `query_hint` read out of config and then dropped would leave every other
    assertion in this file green.
    """
    scripted_chat_model_factory([[answers("Giá vàng SJC đang được niêm yết.")]])
    version = confirmed(web_lookup({"query_hint": "dự báo hôm nay", "country": "vietnam"}))

    response = await trial_workflow(client, version, "giá vàng hôm nay", creator_token)

    assert response.status_code == 200, response.text
    assert a_configured_search_api[0]["query"] == "giá vàng hôm nay dự báo hôm nay"
    assert a_configured_search_api[0]["country"] == "vietnam"


async def test_a_web_search_node_with_no_refinement_searches_the_question_alone(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    a_configured_search_api: list[dict[str, Any]],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """ADR-0042's compatibility half: an unconfigured node behaves exactly as it did before."""
    scripted_chat_model_factory([[answers("Giá vàng SJC đang được niêm yết.")]])
    version = confirmed(web_lookup())

    response = await trial_workflow(client, version, "giá vàng hôm nay", creator_token)

    assert response.status_code == 200, response.text
    assert a_configured_search_api[0]["query"] == "giá vàng hôm nay"
    # Absent, not empty: a node nobody geo-scoped must not ask for nowhere.
    assert "country" not in a_configured_search_api[0]


async def test_a_web_search_node_searches_what_the_node_in_front_of_it_extracted(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    a_configured_search_api: list[dict[str, Any]],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """ADR-0068. The turn is a sentence; what reaches the engine is the company in it.

    The failure this closes, verbatim from the run that reported it: a whole
    conversational sentence went to the provider as a query and came back with
    nothing, because nothing on the web is about a sentence.
    """
    scripted_chat_model_factory([[answers("Công ty Cổ phần Máy bơm ABC")], [answers("Khách là một nhà phân phối.")]])
    version = confirmed(web_lookup_behind_an_extractor())

    response = await trial_workflow(
        client,
        version,
        "Tuần sau em gặp Công ty Cổ phần Máy bơm ABC lần đầu, họ hỏi mua 20 máy bơm. Cho em hồ sơ nhanh về khách.",
        creator_token,
    )

    assert response.status_code == 200, response.text
    assert a_configured_search_api[0]["query"] == "Công ty Cổ phần Máy bơm ABC"


async def test_an_extractor_that_writes_a_paragraph_has_its_first_line_searched(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    a_configured_search_api: list[dict[str, Any]],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """A node told to write search terms that explains itself instead is still usable.

    One line and a bounded one: the query is what a search engine reads, and the
    node's explanation is not part of what this turn is about.
    """
    scripted_chat_model_factory(
        [
            [answers("Công ty Cổ phần Máy bơm ABC\n\nĐây là tên khách hàng mới người dùng vừa nhắc tới.")],
            [answers("Khách là một nhà phân phối.")],
        ]
    )
    version = confirmed(web_lookup_behind_an_extractor())

    response = await trial_workflow(client, version, "Cho em hồ sơ về khách mới.", creator_token)

    assert response.status_code == 200, response.text
    assert a_configured_search_api[0]["query"] == "Công ty Cổ phần Máy bơm ABC"


async def test_a_later_turn_never_searches_the_previous_turns_answer(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    a_configured_search_api: list[dict[str, Any]],
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """Why `spoken` is a per-turn channel and not the newest `AIMessage` (ADR-0068).

    `messages` is the checkpointer's whole thread, so a `WebSearch` node wired
    straight to `start` would find the *first* turn's answer sitting there on the
    second turn and search that instead of what was just asked.
    """
    scripted_chat_model_factory([[answers("Giá vàng SJC đang được niêm yết ở mức cao.")]])
    version = confirmed(web_lookup())
    first = await trial_workflow(client, version, "giá vàng hôm nay", creator_token)
    thread = parse_sse(first.text, event="thread")[0]["conversation_id"]

    scripted_chat_model_factory([[answers("Giá xăng vừa giảm.")]])
    response = await trial_workflow(client, version, "giá xăng hôm nay", creator_token, conversation_id=thread)

    assert response.status_code == 200, response.text
    assert [call["query"] for call in a_configured_search_api] == ["giá vàng hôm nay", "giá xăng hôm nay"]


async def test_a_web_search_node_runs_for_real_and_says_so(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """AC 1's other half. Nothing is configured to search with, so the run reports *that*.

    The point is not the failure but where it comes from: the node reached its
    real implementation and was refused by the real implementation's own missing
    configuration, which is what "read-only tools execute for real" means. A
    sandbox that swapped in a mock would have returned a cheerful `[SANDBOX]`
    line here instead.
    """
    scripted_chat_model_factory([[answers("Chưa tra được gì.")]])
    version = confirmed(web_lookup())

    response = await trial_workflow(client, version, "giá vàng hôm nay", creator_token)

    assert response.status_code == 200, response.text
    results = parse_sse(response.text, event="tool_result")
    assert [result["tool"] for result in results] == ["web_search"]
    assert results[0]["simulated"] is False
    assert results[0]["ok"] is False
    assert "SEARCH_API_KEY" in results[0]["result"]


# --------------------------------------------------------------------------- #
# AC 3 — what the run cost
# --------------------------------------------------------------------------- #


async def test_the_creator_is_told_the_tokens_the_time_and_the_price(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """AC 3. All three, measured — a figure never travels without saying what kind it is."""
    scripted_chat_model_factory([[answers("Hoàn tiền trong 7 ngày làm việc.", tokens=500)]])
    version = confirmed()

    response = await trial_workflow(client, version, "hoàn tiền mất bao lâu", creator_token)

    costs = parse_sse(response.text, event="cost")
    assert len(costs) == 1, response.text
    assert costs[0]["input_tokens"] + costs[0]["output_tokens"] == 500
    assert costs[0]["seconds"] is not None
    assert costs[0]["usd"] > 0
    assert costs[0]["basis"] == "measured"


# --------------------------------------------------------------------------- #
# AC 4 — the gate `mark_sandbox_passed` guards
# --------------------------------------------------------------------------- #


async def test_a_finished_run_is_what_moves_a_confirmed_workflow_to_sandbox_passed(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    db_session: Session,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """AC 4. A V2 version never passes through `draft`, so `workflow_confirmed` is its door."""
    scripted_chat_model_factory([[answers("Hoàn tiền trong 7 ngày làm việc.")]])
    version = confirmed()

    response = await trial_workflow(client, version, "hoàn tiền mất bao lâu", creator_token)

    record = sandbox_record(response)
    assert record["status"] == SandboxRunStatus.SUCCEEDED.value
    assert record["version_status"] == VersionStatus.SANDBOX_PASSED.value
    db_session.expire_all()
    assert db_session.get(AgentVersion, version.id).status is VersionStatus.SANDBOX_PASSED


async def test_a_run_stopped_by_its_budget_does_not_earn_sandbox_passed(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    db_session: Session,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """A truncated transcript proves nothing about the nodes it never reached (#10)."""
    scripted_chat_model_factory([[answers("Hoàn tiền trong 7 ngày làm việc.", tokens=500)]])
    version = confirmed(document_qa(documents, budget={"max_total_tokens": 10, "max_wall_seconds": 60}))

    response = await trial_workflow(client, version, "hoàn tiền mất bao lâu", creator_token)

    record = sandbox_record(response)
    assert record["status"] == SandboxRunStatus.FAILED.value
    assert record["version_status"] == VersionStatus.WORKFLOW_CONFIRMED.value
    assert record["error"]
    # In the transcript too, not only in the run's `error`: this is where an
    # admin reads down looking for where the run stopped.
    saved = await stored_run(client, version, record["run_id"], creator_token)
    transcript = saved.json()["transcript"]
    assert [line["kind"] for line in transcript][-1] == "budget_exceeded"


async def test_re_running_a_version_that_already_passed_does_not_drag_it_backwards(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """A creator retrying must not be refused, and a version past the gate stays where it is."""
    scripted_chat_model_factory([[answers("Lần một.")], [answers("Lần hai.")]])
    version = confirmed(status=VersionStatus.SANDBOX_PASSED)

    response = await trial_workflow(client, version, "hoàn tiền mất bao lâu", creator_token)

    assert response.status_code == 200, response.text
    record = sandbox_record(response)
    assert record["status"] == SandboxRunStatus.SUCCEEDED.value
    assert record["version_status"] == VersionStatus.SANDBOX_PASSED.value


async def test_a_workflow_nobody_has_confirmed_cannot_be_trialled(
    client: AsyncClient, creator_token: str, confirmed: Callable[..., AgentVersion], documents: str
) -> None:
    """The same 409 the chat door gives: an editable graph is not evidence of anything."""
    version = confirmed(status=VersionStatus.WORKFLOW_PENDING_CONFIRMATION)

    response = await trial_workflow(client, version, "hoàn tiền mất bao lâu", creator_token)

    assert response.status_code == 409, response.text
    assert VersionStatus.WORKFLOW_PENDING_CONFIRMATION.value in response.json()["detail"]


async def test_only_the_owner_can_trial_a_version(
    client: AsyncClient,
    second_creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
) -> None:
    """404, the way every version read is — an id must not leak that it exists."""
    response = await trial_workflow(client, confirmed(), "hoàn tiền mất bao lâu", second_creator_token)

    assert response.status_code == 404, response.text


# --------------------------------------------------------------------------- #
# AC 5 — where a rehearsal's memory goes
# --------------------------------------------------------------------------- #


async def test_a_trial_run_lands_on_a_trial_thread_and_never_on_production(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """AC 5. Test chatter must not become the memory a real conversation continues from."""
    scripted_chat_model_factory([[answers("Hoàn tiền trong 7 ngày làm việc.")]])
    version = confirmed()

    response = await trial_workflow(client, version, "hoàn tiền mất bao lâu", creator_token)

    threads = parse_sse(response.text, event="thread")
    assert [thread["namespace"] for thread in threads] == ["trial"]
    assert sandbox_record(response)["run_id"]


async def test_the_stream_opens_with_the_thread_and_closes_with_the_record(
    client: AsyncClient,
    creator_token: str,
    confirmed: Callable[..., AgentVersion],
    documents: str,
    scripted_chat_model_factory: Callable[[list[list[AIMessage]]], ScriptedRun],
) -> None:
    """The order a client depends on: know the conversation first, the evidence last."""
    scripted_chat_model_factory([[answers("Hoàn tiền trong 7 ngày làm việc.")]])
    version = confirmed()

    response = await trial_workflow(client, version, "hoàn tiền mất bao lâu", creator_token)

    names = [name for name, _payload in parse_sse_stream(response.text)]
    assert names[0] == "thread"
    assert names[-1] == "sandbox_run"
    assert names[-2] == "cost"
