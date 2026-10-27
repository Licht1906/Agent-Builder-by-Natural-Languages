"""A business-data need, described in words, becomes a `data_query` node — DS-11 / ADR-0059 §5, §7.

The ticket's four acceptance criteria:

- the demo description is read with a `database` source (the Designer's
  prompt and the kind it normalises to);
- once the creator attaches the Hệ CSDL, the drafted canvas is
  `collect → data_query → agent_llm → human_approval → <action>`, its
  `data_query` bound to that Hệ CSDL and its views **by rule** — whatever the
  model wrote there is discarded;
- removing the `human_approval` is red on the canvas and cannot be confirmed;
- a description about documents alone never grows a `data_query`.

The model is a scripted double (`ScriptedMetaAgent`), so what is measured is
the platform around it: what it keeps, what it overwrites, what it refuses.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from langchain_core.messages import AIMessage

from src.db.models import AgentVersion, DatabaseSource, User, VersionStatus
from src.models.node_registry import NodeType
from src.models.solution_spec import AgentSolutionSpecification, DataSourceKind
from src.models.workflow import Edge, NodeInstance, WorkflowJSON
from src.solution.designer import _DESIGNER_PROMPT
from src.solution.draft import SpecDraft
from src.workflow.composition import _shape_complaints
from tests.conftest import ScriptedMetaAgent, auth_header, install_chat_model_factory
from tests.test_api.test_data_query_node import _views, source

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("_views")]

# Fixtures borrowed from the module that owns the scenery, named so pytest sees them.
__all__ = ["_views", "source"]

#: Spec §Kịch bản demo bước 2, as the Designer reads it once the creator has
#: attached their Hệ CSDL and their Gmail.
DEMO = "Tạo trợ lý công nợ cho phòng Kinh doanh. Nhân viên chỉ xem công nợ của khách mình phụ trách, trưởng nhóm xem cả phòng. Khi khách quá hạn trên 30 ngày thì soạn email nhắc nợ gửi khách."


def _spec(source_ref: str | None) -> dict[str, Any]:
    return {
        "goal": "Trợ lý công nợ cho phòng Kinh doanh, soạn và gửi email nhắc nợ khách quá hạn trên 30 ngày",
        "users": ["Nhân viên kinh doanh", "Trưởng nhóm kinh doanh"],
        "data": [
            {"name": "ERP mẫu — công nợ khách hàng", "kind": "database", "ref": source_ref},
            {"name": "Hộp thư Gmail của phòng", "kind": "connected_account", "ref": "conn://gmail-1"},
        ],
        "actions": ["Tra công nợ của khách", "Soạn email nhắc nợ", "Gửi email nhắc nợ cho khách sau khi duyệt"],
        "expected_outcome": ["Một email nhắc nợ đã gửi cho khách quá hạn"],
        "success_criteria": [{"text": "Email nêu đúng số tiền và số ngày quá hạn", "category": "correctness"}],
        "prohibitions": ["Không gửi khi chưa được duyệt"],
    }


#: What the composer draws for the demo — with a `data_query` config the model
#: had no business writing, to show it does not survive.
DEMO_GRAPH: dict[str, Any] = {
    "start_node_id": "thu_thap",
    "nodes": [
        {"id": "thu_thap", "type": "collect", "config": {"action_node": "gui", "agent_writes": ["subject", "body"]}},
        {"id": "doc", "type": "data_query", "config": {"source_id": "999", "views": ["fin_invoice"]}},
        {"id": "soan", "type": "agent_llm", "config": {"prompt": "Soạn email nhắc nợ từ các dòng công nợ đọc được."}},
        {
            "id": "duyet",
            "type": "human_approval",
            "config": {"approver_role": "manager", "message_template": "Đây là email sẽ gửi, bạn đồng ý gửi không?"},
        },
        {"id": "gui", "type": "mcp", "config": {"action": "send_email"}},
        {"id": "bao", "type": "agent_llm", "config": {"prompt": "Báo lại email đã gửi hay chưa và vì sao."}},
        {"id": "xong", "type": "end", "config": {}},
    ],
    "edges": [
        {"id": "e1", "source_node_id": "thu_thap", "source_port": "complete", "target_node_id": "doc"},
        {"id": "e2", "source_node_id": "thu_thap", "source_port": "incomplete", "target_node_id": "xong"},
        {"id": "e3", "source_node_id": "doc", "source_port": "done", "target_node_id": "soan"},
        {"id": "e4", "source_node_id": "soan", "source_port": "done", "target_node_id": "duyet"},
        {"id": "e5", "source_node_id": "duyet", "source_port": "approved", "target_node_id": "gui"},
        {"id": "e6", "source_node_id": "duyet", "source_port": "rejected", "target_node_id": "bao"},
        {"id": "e7", "source_node_id": "gui", "source_port": "done", "target_node_id": "bao"},
        {"id": "e8", "source_node_id": "bao", "source_port": "done", "target_node_id": "xong"},
    ],
}


def _replies(*documents: dict[str, Any]) -> ScriptedMetaAgent:
    model = ScriptedMetaAgent(*(AIMessage(content=json.dumps(document, ensure_ascii=False)) for document in documents))
    install_chat_model_factory(model)
    return model


async def _draft(client: AsyncClient, token: str, row: AgentVersion, **body: Any) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/agents/{row.agent_id}/versions/{row.id}/workflow", headers=auth_header(token), json=body or None
    )
    assert response.status_code == 201, response.text
    return response.json()["workflow"]


@pytest.fixture
def confirmed_spec(
    solution_spec_version: Callable[..., AgentVersion], seeded_creator: User
) -> Callable[[dict[str, Any]], AgentVersion]:
    def _make(spec: dict[str, Any]) -> AgentVersion:
        return solution_spec_version(seeded_creator, spec, status=VersionStatus.SPEC_CONFIRMED, name="Trợ lý công nợ")

    return _make


def _order(workflow: dict[str, Any]) -> list[str]:
    """Node types along the `complete → done → approved → done` spine, from the start."""
    by_id = {node["id"]: node for node in workflow["nodes"]}
    onward = {
        (edge["source_node_id"]): edge["target_node_id"]
        for edge in workflow["edges"]
        if edge["source_port"] in ("complete", "done", "found", "approved")
    }
    order, current = [], workflow["start_node_id"]
    while current in by_id and len(order) < 20:
        order.append(by_id[current]["type"])
        current = onward.get(current)
    return order


# --------------------------------------------------------------------------- #
# The Designer reads a business-data need as `database`
# --------------------------------------------------------------------------- #


def test_the_designer_is_told_what_a_database_source_is() -> None:
    assert "= database" in _DESIGNER_PROMPT
    for example in ("công nợ", "đơn hàng", "tồn kho"):
        assert example in _DESIGNER_PROMPT


@pytest.mark.parametrize("written", ["database", "ERP", "csdl", "Hệ CSDL", "SQL"])
def test_a_database_however_the_model_spells_it_is_a_database_source(written: str) -> None:
    draft = SpecDraft.read({"data": [{"name": "Công nợ khách hàng", "kind": written}]})

    assert [(source.name, source.kind) for source in draft.data] == [("Công nợ khách hàng", DataSourceKind.DATABASE)]


# --------------------------------------------------------------------------- #
# The Generator draws the demo shape and binds the Hệ CSDL by rule
# --------------------------------------------------------------------------- #


async def test_the_demo_spec_drafts_collect_query_draft_approve_send_bound_to_the_attached_source(
    client: AsyncClient,
    creator_token: str,
    confirmed_spec: Callable[[dict[str, Any]], AgentVersion],
    source: DatabaseSource,
) -> None:
    _replies(DEMO_GRAPH)
    row = confirmed_spec(_spec(str(source.id)))

    workflow = await _draft(client, creator_token, row)

    assert _order(workflow) == ["collect", "data_query", "agent_llm", "human_approval", "mcp", "agent_llm", "end"]
    [query] = [node for node in workflow["nodes"] if node["type"] == "data_query"]
    # The model's "999" and "fin_invoice" are gone: the attached source, and its
    # ticked views less the employee view the access rules resolve askers in.
    assert query["config"]["source_id"] == str(source.id)
    assert query["config"]["views"] == ["v_dqn_ar"]


async def test_a_source_not_attached_yet_leaves_its_node_for_the_canvas_to_ring(
    client: AsyncClient, creator_token: str, confirmed_spec: Callable[[dict[str, Any]], AgentVersion]
) -> None:
    _replies(DEMO_GRAPH)
    row = confirmed_spec(_spec(None))

    workflow = await _draft(client, creator_token, row)

    [query] = [node for node in workflow["nodes"] if node["type"] == "data_query"]
    assert (query["config"]["source_id"], query["config"]["views"]) == ("", [])


async def test_a_source_that_is_not_the_creators_is_never_bound(
    client: AsyncClient, creator_token: str, confirmed_spec: Callable[[dict[str, Any]], AgentVersion]
) -> None:
    _replies(DEMO_GRAPH)
    row = confirmed_spec(_spec("424242"))

    workflow = await _draft(client, creator_token, row)

    [query] = [node for node in workflow["nodes"] if node["type"] == "data_query"]
    assert query["config"]["source_id"] == ""


async def test_the_gallery_template_has_the_same_shape_and_is_bound_the_same_way(
    client: AsyncClient,
    creator_token: str,
    confirmed_spec: Callable[[dict[str, Any]], AgentVersion],
    source: DatabaseSource,
) -> None:
    _replies({"nodes": {"draft": {"prompt": "Soạn email nhắc nợ."}}})
    row = confirmed_spec(_spec(str(source.id)))

    workflow = await _draft(client, creator_token, row, template_id="database_outreach")

    assert _order(workflow) == ["collect", "data_query", "agent_llm", "human_approval", "mcp", "agent_llm", "end"]
    [query] = [node for node in workflow["nodes"] if node["type"] == "data_query"]
    assert (query["config"]["source_id"], query["config"]["views"]) == (str(source.id), ["v_dqn_ar"])


async def test_without_its_approval_the_drafted_graph_is_red_and_cannot_be_confirmed(
    client: AsyncClient,
    creator_token: str,
    confirmed_spec: Callable[[dict[str, Any]], AgentVersion],
    source: DatabaseSource,
) -> None:
    """AC: "xoá human_approval → validator báo đỏ, không xác nhận được"."""
    _replies(DEMO_GRAPH)
    row = confirmed_spec(_spec(str(source.id)))
    workflow = await _draft(client, creator_token, row)

    # The creator binds the send on the canvas — `action` is theirs to pick, never
    # derived — then deletes the approval and wires the draft straight to the send.
    approval = next(node["id"] for node in workflow["nodes"] if node["type"] == "human_approval")
    send = next(node["id"] for node in workflow["nodes"] if node["type"] == "mcp")
    for node in workflow["nodes"]:
        if node["id"] == send:
            node["config"]["action"] = "send_email"
    workflow["nodes"] = [node for node in workflow["nodes"] if node["id"] != approval]
    workflow["edges"] = [
        {**edge, "target_node_id": send} if edge["target_node_id"] == approval else edge
        for edge in workflow["edges"]
        if edge["source_node_id"] != approval
    ]
    base = f"/api/v1/agents/{row.agent_id}/versions/{row.id}/workflow"
    saved = await client.put(base, json=workflow, headers=auth_header(creator_token))
    assert saved.status_code == 200, saved.text
    assert any("Human Approval" in problem["message"] for problem in saved.json()["problems"]), [
        problem["message"] for problem in saved.json()["problems"]
    ]

    confirmed = await client.post(f"{base}/confirm", headers=auth_header(creator_token))

    assert confirmed.status_code == 409, confirmed.text
    assert "Human Approval" in confirmed.json()["detail"]


# --------------------------------------------------------------------------- #
# Documents alone never grow a data_query — and the composer is told so
# --------------------------------------------------------------------------- #


def _graph(nodes: list[tuple[str, NodeType]], edges: list[tuple[str, str, str]]) -> WorkflowJSON:
    return WorkflowJSON(
        start_node_id=nodes[0][0],
        nodes=[NodeInstance(id=node_id, type=node_type, config={}) for node_id, node_type in nodes],
        edges=[Edge(id=f"{a}-{p}-{b}", source_node_id=a, source_port=p, target_node_id=b) for a, p, b in edges],
    )


def _documents_spec(**data: Any) -> AgentSolutionSpecification:
    return AgentSolutionSpecification.model_validate(
        {
            "goal": "Trả lời câu hỏi về chính sách bán hàng",
            "users": ["Nhân viên kinh doanh"],
            "data": [data or {"name": "Sổ tay chính sách", "kind": "uploaded_file", "ref": "1"}],
            "actions": ["Tra chính sách"],
            "expected_outcome": ["Câu trả lời có trích dẫn"],
            "success_criteria": [{"text": "Bám tài liệu", "category": "source_groundedness"}],
            "prohibitions": ["Không suy đoán"],
        }
    )


def test_a_graph_reading_a_database_the_spec_does_not_have_is_sent_back() -> None:
    graph = _graph(
        [
            ("tim", NodeType.KNOWLEDGE_SEARCH),
            ("doc", NodeType.DATA_QUERY),
            ("tra_loi", NodeType.AGENT_LLM),
            ("xong", NodeType.END),
        ],
        [("tim", "found", "doc"), ("doc", "done", "tra_loi"), ("tra_loi", "done", "xong")],
    )

    assert "Bài toán không có Hệ CSDL nào; bỏ node data_query" in _shape_complaints(
        graph, _documents_spec(), frozenset()
    )


def test_a_database_spec_drawn_without_a_data_query_is_sent_back() -> None:
    spec = _documents_spec(name="Công nợ trong ERP", kind="database", ref="1")
    graph = _graph([("tra_loi", NodeType.AGENT_LLM), ("xong", NodeType.END)], [("tra_loi", "done", "xong")])

    complaints = _shape_complaints(graph, spec, frozenset())

    assert any("không có node data_query nào" in complaint for complaint in complaints)


def test_two_data_query_nodes_for_one_database_are_sent_back() -> None:
    spec = _documents_spec(name="Công nợ trong ERP", kind="database", ref="1")
    graph = _graph(
        [
            ("a", NodeType.DATA_QUERY),
            ("b", NodeType.DATA_QUERY),
            ("tra_loi", NodeType.AGENT_LLM),
            ("xong", NodeType.END),
        ],
        [("a", "done", "tra_loi"), ("b", "done", "tra_loi"), ("tra_loi", "done", "xong")],
    )

    assert any(
        "2 node data_query cho 1 Hệ CSDL" in complaint for complaint in _shape_complaints(graph, spec, frozenset())
    )


async def test_a_documents_only_spec_drafted_with_a_data_query_is_redrawn_without_one(
    client: AsyncClient,
    creator_token: str,
    solution_spec_version: Callable[..., AgentVersion],
    seeded_creator: User,
) -> None:
    """AC: "mô tả thuần tài liệu không sinh data_query" — the composer is told, and draws again."""
    spec = _documents_spec().model_dump(mode="json")
    with_query = {
        "start_node_id": "tim",
        "nodes": [
            {"id": "tim", "type": "knowledge_search", "config": {}},
            {"id": "doc", "type": "data_query", "config": {}},
            {"id": "tra_loi", "type": "agent_llm", "config": {"prompt": "Trả lời theo tài liệu."}},
            {"id": "xong", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "tim", "source_port": "found", "target_node_id": "doc"},
            {"id": "e2", "source_node_id": "doc", "source_port": "done", "target_node_id": "tra_loi"},
            {"id": "e3", "source_node_id": "tra_loi", "source_port": "done", "target_node_id": "xong"},
        ],
    }
    without = {
        "start_node_id": "tim",
        "nodes": [node for node in with_query["nodes"] if node["id"] != "doc"],
        "edges": [
            {"id": "e1", "source_node_id": "tim", "source_port": "found", "target_node_id": "tra_loi"},
            {"id": "e3", "source_node_id": "tra_loi", "source_port": "done", "target_node_id": "xong"},
        ],
    }
    model = _replies(with_query, without)
    row = solution_spec_version(seeded_creator, spec, status=VersionStatus.SPEC_CONFIRMED, name="Tra chính sách")

    workflow = await _draft(client, creator_token, row)

    assert "data_query" not in {node["type"] for node in workflow["nodes"]}
    assert "bỏ node data_query" in model.prompts[1]


# --------------------------------------------------------------------------- #
# The golden set carries the same claims for the workbench meta-agent
# --------------------------------------------------------------------------- #


def _golden(case_id: str) -> Any:
    """One case of DS-11's own golden file — kept apart from the gate's pre-registered set (golden/README.md)."""
    from pathlib import Path

    from src.evals.meta_agent_gate import load_cases

    path = Path(__file__).resolve().parents[2] / "golden" / "meta_agent_data_source_v1.jsonl"
    return next(case for case in load_cases(path) if case.id == case_id)


def test_the_golden_demo_case_is_met_by_the_demo_shape() -> None:
    from src.evals.meta_agent_gate import grade
    from src.metaagent.loop import TurnReply

    case = _golden("draft-db-01")
    assert case.turns == (DEMO,) or list(case.turns) == [DEMO]
    graph = WorkflowJSON.model_validate(DEMO_GRAPH)

    verdict = grade(
        case,
        TurnReply(loi_nhan="Đã dựng.", workflow=graph),
        before=WorkflowJSON.model_validate(
            {"start_node_id": "x", "nodes": [{"id": "x", "type": "end", "config": {}}], "edges": []}
        ),
    )

    assert verdict.failures == ()


def test_the_golden_documents_case_fails_a_graph_that_reads_a_database() -> None:
    from src.evals.meta_agent_gate import grade
    from src.metaagent.loop import TurnReply

    graph = _graph(
        [
            ("tim", NodeType.KNOWLEDGE_SEARCH),
            ("doc", NodeType.DATA_QUERY),
            ("tra_loi", NodeType.AGENT_LLM),
            ("xong", NodeType.END),
        ],
        [("tim", "found", "doc"), ("doc", "done", "tra_loi"), ("tra_loi", "done", "xong")],
    )
    empty = WorkflowJSON.model_validate(
        {"start_node_id": "x", "nodes": [{"id": "x", "type": "end", "config": {}}], "edges": []}
    )

    verdict = grade(_golden("draft-db-03"), TurnReply(loi_nhan="Đã dựng.", workflow=graph), before=empty)

    assert "forbidden_node_types: data_query present" in verdict.failures
