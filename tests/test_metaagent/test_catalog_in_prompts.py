"""What the meta agent is told about an action: its parameters, and the rules that follow.

The field case that produced all of this: an invitation agent whose "đã đủ
thông tin?" gate demanded *email người chủ trì*, a field `gmail.send_email`
has no parameter for — the sender is the connected account. A user who gave
everything the action could use was refused for it. The model that wrote the
gate had been shown *what* the action does and never *what it takes*.

These tests pin the repair at the level the mistake was made: both prompts
(the workbench loop's and the composer's) carry each action's parameters,
required ones starred — and, since ADR-0052, four principles in place of the
rule-per-incident list that grew out of the first eleven Google agents, with
the gathering itself handed to the `collect` node so that no prompt names a
parameter at all.
"""

import asyncio
from collections.abc import Sequence

from langchain_core.messages import AIMessage

from src.metaagent.catalog import catalog_lines, creator_catalog, parameters_of
from src.metaagent.loop import CatalogAction, CatalogTool, TurnRequest, run_turn
from src.models.model_catalog import DEFAULT_MODEL_ALLOWLIST
from src.models.solution_spec import AgentSolutionSpecification
from src.models.workflow import WorkflowJSON
from src.workflow.composition import _composition_prompt

SEND = CatalogAction(
    name="send_email",
    description="Gửi email tới người nhận — không lấy lại được",
    read_only=False,
    risk="high",
    input_schema={
        "type": "object",
        "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
        "required": ["to", "subject", "body"],
    },
)
SEARCH = CatalogAction(
    name="search_emails",
    description="Tìm thư",
    read_only=True,
    risk="low",
    input_schema={"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}},
)
GMAIL = CatalogTool(id="gmail", name="Gmail", actions=(SEND, SEARCH))


def test_parameters_are_listed_with_required_ones_starred() -> None:
    assert parameters_of(SEND.input_schema) == "to*, subject*, body*"
    assert parameters_of(SEARCH.input_schema) == "query, max_results"
    assert parameters_of({}) == "không có tham số"


def test_catalog_lines_carry_effect_description_and_parameters() -> None:
    lines = catalog_lines((GMAIL,))

    assert lines == [
        "- gmail.send_email (WRITE, risk high) — Gửi email tới người nhận — không lấy lại được; tham số: to*, subject*, body*",
        "- gmail.search_emails (read-only) — Tìm thư; tham số: query, max_results",
    ]


class _Scripted:
    def __init__(self, replies: Sequence[AIMessage]) -> None:
        self.replies = list(replies)
        self.seen: list[list] = []

    def bind_tools(self, tools):  # noqa: ANN001, ANN201 — duck-typed BaseChatModel surface
        return self

    async def ainvoke(self, messages):  # noqa: ANN001, ANN201
        self.seen.append(list(messages))
        return self.replies.pop(0)


_EMPTY_GRAPH = WorkflowJSON.model_validate(
    {"start_node_id": "done", "nodes": [{"id": "done", "type": "end", "config": {}}], "edges": []}
)


def _spec() -> AgentSolutionSpecification:
    return AgentSolutionSpecification.model_validate(
        {
            "goal": "Gửi thư mời tham dự sự kiện từ người chủ trì đến người được mời qua email.",
            "users": ["nhân viên văn phòng"],
            "data": [{"name": "thông tin sự kiện", "kind": "user_input", "ref": None}],
            "actions": ["Gửi email lời mời từ người chủ trì đến người được mời."],
            "expected_outcome": ["email đã gửi"],
            "success_criteria": [{"text": "làm đúng yêu cầu", "category": "correctness"}],
            "prohibitions": ["không bịa"],
        }
    )


def _loop_prompt() -> str:
    model = _Scripted([AIMessage(content="Tôi sẽ dựng sau.")])
    asyncio.run(run_turn(model, TurnRequest(message="dựng agent mời tiệc", workflow=_EMPTY_GRAPH, catalog=(GMAIL,))))  # type: ignore[arg-type]
    return str(model.seen[0][0].content)


def _composer_prompt() -> str:
    return _composition_prompt(spec=_spec(), model_allowlist=list(DEFAULT_MODEL_ALLOWLIST), catalog=(GMAIL,))


def test_both_prompts_carry_the_catalog_with_parameters() -> None:
    for prompt in (_loop_prompt(), _composer_prompt()):
        assert "gmail.send_email (WRITE, risk high)" in prompt
        assert "tham số: to*, subject*, body*" in prompt


def test_both_prompts_state_the_four_principles_rather_than_a_rule_per_incident() -> None:
    """The rules were accreting one per field failure; ADR-0052 folds them into four principles.

    Each principle is asserted by its heading sentence, in both prompts — the
    composer (Vietnamese) and the workbench loop (English) must say the same
    four things, or the two meta agents drift into building different graphs
    for one spec.
    """
    composer = _composer_prompt()
    assert "MỘT LƯỢT CHAT LÀ MỘT LẦN CHẠY" in composer
    assert "GRAPH ĐI THEO SCHEMA CỦA ACTION" in composer
    assert "MỖI NODE CHỈ NÓI ĐIỀU CÓ TRONG ĐẦU VÀO CỦA NÓ" in composer
    assert "BẢNG DỮ LIỆU → NODE, HIỆU ỨNG → DUYỆT" in composer

    loop = _loop_prompt()
    assert "**One chat turn is one run**" in loop
    assert "**The graph follows the action's schema, not the problem's prose.**" in loop
    assert "**A node says only what is in its input.**" in loop
    assert "**Data kind → node type; effect → approval.**" in loop


def test_gathering_is_the_collect_nodes_job_and_no_prompt_names_parameters() -> None:
    """What replaced the collector prose: the `collect` node (ADR-0052), and three prohibitions."""
    composer = _composer_prompt()
    assert '"collect": cổng ra [complete, incomplete]' in composer  # the catalogue line, generated
    assert 'Node "collect" CHỈ tồn tại khi' in composer
    assert 'KHÔNG viết node "agent_llm" để thu thập' in composer
    assert 'KHÔNG viết "if_else" "đã đủ thông tin?"' in composer
    assert "KHÔNG kể tên tham số" in composer
    assert '"agent_writes"' in composer
    assert "tài khoản thực hiện action là tài khoản đã nối của chính người tạo" in composer

    loop = _loop_prompt()
    assert "`collect` (ports: complete, incomplete" in loop
    assert "Gathering details from the user is the `collect` node's job" in loop
    assert "do NOT write an `agent_llm` collector" in loop
    assert "do NOT name parameters in any" in loop
    assert "the action runs as the creator's own" in loop


def test_grounding_covers_both_directions_add_nothing_drop_nothing() -> None:
    """Two field cases under one principle: the invented `…/d/your_document_id_here` link, and a
    summariser that filtered a live listing by "tuần trước" and reported nothing found."""
    composer = _composer_prompt()
    assert "KHÔNG THÊM" in composer and "KHÔNG BỚT" in composer
    assert '"tuần trước"' in composer
    loop = _loop_prompt()
    assert "adds nothing" in loop and "drops" in loop
    assert "last week" in " ".join(loop.split())  # the source wraps the line inside the quotes


def test_the_data_to_node_table_and_the_field_contracts_survive() -> None:
    composer = _composer_prompt()
    assert 'connected_account: Gmail, Drive…) → "mcp"' in composer
    assert 'không bao giờ "knowledge_search" cho một tài khoản' in composer
    assert 'Action "(read-only)" không cần "human_approval"' in composer
    assert "KHÔNG có cú pháp thay" in composer
    # The example placeholders in the contract survive `str.format` as the model must read them.
    assert '"{{...}}", "{to}"' in composer

    loop = _loop_prompt()
    assert "never `knowledge_search` for an account" in loop
    assert "NO substitution syntax" in loop


def test_the_composer_without_a_catalog_is_told_so_rather_than_handed_a_blank() -> None:
    prompt = _composition_prompt(spec=_spec(), model_allowlist=list(DEFAULT_MODEL_ALLOWLIST))

    assert "KHÔNG vẽ node mcp, KHÔNG vẽ node collect" in prompt


def test_creator_catalog_carries_the_built_in_schemas(db_session) -> None:  # noqa: ANN001
    catalog = creator_catalog(db_session, creator_id=1)

    gmail = next(tool for tool in catalog if tool.id == "gmail")
    send = gmail.action("send_email")
    assert send is not None
    assert send.input_schema["required"] == ["to", "subject", "body"]
    drive = next(tool for tool in catalog if tool.id == "google_drive")
    assert drive.action("create_doc") is not None
