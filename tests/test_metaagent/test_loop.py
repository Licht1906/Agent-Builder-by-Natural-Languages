"""`src.metaagent.loop` — the turn loop, driven by a scripted model.

Every test scripts the model's replies and asserts on what the loop *did*:
which safety wall held, what the trail recorded, how the turn concluded. No
network, no database, no real model — the loop's whole contract is exercisable
because everything it needs arrives as arguments.
"""

import asyncio
from collections.abc import Sequence

from langchain_core.messages import AIMessage, ToolMessage

from src.metaagent.loop import (
    MAX_TOOL_CALLS,
    CatalogAction,
    CatalogTool,
    HistoryTurn,
    TurnRequest,
    run_turn,
)
from src.models.workflow import WorkflowJSON


class ScriptedModel:
    """A model whose replies are written in advance — the loop's one seam."""

    def __init__(self, replies: Sequence[AIMessage]) -> None:
        self.replies = list(replies)
        self.seen: list[list] = []

    def bind_tools(self, tools):  # noqa: ANN001, ANN201 — duck-typed BaseChatModel surface
        self.tools = tools
        return self

    async def ainvoke(self, messages):  # noqa: ANN001, ANN201
        self.seen.append(list(messages))
        return self.replies.pop(0)


def _run(model: ScriptedModel, request: TurnRequest):
    return asyncio.run(run_turn(model, request))  # type: ignore[arg-type]


def _call(name: str, args: dict, call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _ket_thuc(loi_nhan: str = "Đã xong.", **extra) -> AIMessage:
    return _call("ket_thuc", {"loi_nhan": loi_nhan, **extra})


GRAPH = {
    "start_node_id": "search",
    "nodes": [
        {"id": "search", "type": "knowledge_search", "config": {"source_ids": ["so-tay.pdf"], "query_template": "x"}},
        {
            "id": "answer",
            "type": "agent_llm",
            "config": {"prompt": "Tóm tắt", "model": "gpt-4o-mini"},
            "position": {"x": 300, "y": 60},
        },
        {"id": "done", "type": "end", "config": {}},
    ],
    "edges": [
        {"id": "e1", "source_node_id": "search", "source_port": "found", "target_node_id": "answer"},
        {"id": "e2", "source_node_id": "answer", "source_port": "done", "target_node_id": "done"},
    ],
}

CATALOG = (
    CatalogTool(
        id="gmail",
        name="Gmail",
        actions=(
            CatalogAction(name="send_email", description="Gửi email", read_only=False, risk="high"),
            CatalogAction(name="search_emails", description="Tìm thư", read_only=True, risk="low"),
        ),
    ),
)


def request(**overrides) -> TurnRequest:
    base = {"message": "sửa giúp tôi", "workflow": WorkflowJSON.model_validate(GRAPH)}
    return TurnRequest(**{**base, **overrides})


def test_bare_text_concludes_the_turn() -> None:
    """A model that answers prose without tools has answered — no wedge, no crash."""
    reply = _run(ScriptedModel([AIMessage(content="Tôi chưa hiểu ý bạn.")]), request())
    assert reply.loi_nhan == "Tôi chưa hiểu ý bạn."
    assert reply.workflow is None
    assert reply.cau_hoi is None


def test_ket_thuc_with_question_reaches_the_reply() -> None:
    reply = _run(ScriptedModel([_ket_thuc("Cần hỏi đã.", cau_hoi="Gửi cho ai?")]), request())
    assert reply.cau_hoi == "Gửi cho ai?"
    assert reply.workflow is None


def test_proposal_ships_and_the_trail_records_it() -> None:
    proposed = {**GRAPH, "nodes": [*GRAPH["nodes"]]}
    model = ScriptedModel([_call("de_xuat_graph", {"workflow": proposed, "ghi_chu": "giữ nguyên"}), _ket_thuc()])
    reply = _run(model, request())
    assert reply.workflow is not None
    assert reply.van_de == ()
    assert [step.tool for step in reply.cac_buoc] == ["de_xuat_graph"]
    assert reply.cac_buoc[0].ok


def test_source_ids_never_survive_the_model() -> None:
    """The model rewrites a knowledge node's attachments; restoration puts the
    creator's own back — tier two of the resource rule."""
    stolen = {
        **GRAPH,
        "nodes": [
            {
                "id": "search",
                "type": "knowledge_search",
                "config": {"source_ids": ["tai-lieu-cua-nguoi-khac.pdf"], "query_template": "x"},
            },
            *GRAPH["nodes"][1:],
        ],
    }
    model = ScriptedModel([_call("de_xuat_graph", {"workflow": stolen}), _ket_thuc()])
    reply = _run(model, request())
    search = next(node for node in reply.workflow.nodes if node.id == "search")
    assert search.config["source_ids"] == ["so-tay.pdf"]


def test_existing_positions_survive_the_model() -> None:
    moved = {
        **GRAPH,
        "nodes": [
            GRAPH["nodes"][0],
            {**GRAPH["nodes"][1], "position": {"x": 9999, "y": 9999}},
            GRAPH["nodes"][2],
        ],
    }
    model = ScriptedModel([_call("de_xuat_graph", {"workflow": moved}), _ket_thuc()])
    reply = _run(model, request())
    answer = next(node for node in reply.workflow.nodes if node.id == "answer")
    assert (answer.position.x, answer.position.y) == (300, 60)


def _with_mcp(tool_id: str, action: str) -> dict:
    return {
        "start_node_id": "search",
        "nodes": [
            *GRAPH["nodes"][:2],
            {"id": "send", "type": "mcp", "config": {"tool_id": tool_id, "action": action}},
            GRAPH["nodes"][2],
        ],
        "edges": [
            GRAPH["edges"][0],
            {"id": "e2", "source_node_id": "answer", "source_port": "done", "target_node_id": "send"},
            {"id": "e3", "source_node_id": "send", "source_port": "done", "target_node_id": "done"},
        ],
    }


def test_catalog_matched_tool_reference_survives() -> None:
    """Tier one: an exact catalog pair proposed by the model is kept."""
    model = ScriptedModel([_call("de_xuat_graph", {"workflow": _with_mcp("gmail", "send_email")}), _ket_thuc()])
    reply = _run(model, request(catalog=CATALOG))
    send = next(node for node in reply.workflow.nodes if node.id == "send")
    assert send.config["tool_id"] == "gmail"
    assert send.config["action"] == "send_email"


def test_invented_tool_reference_is_blanked() -> None:
    model = ScriptedModel([_call("de_xuat_graph", {"workflow": _with_mcp("slack", "post_message")}), _ket_thuc()])
    reply = _run(model, request(catalog=CATALOG))
    send = next(node for node in reply.workflow.nodes if node.id == "send")
    assert send.config["tool_id"] == ""
    assert send.config["action"] == ""


def test_validator_verdict_reaches_the_model() -> None:
    """A proposal that strands a node comes back as sentences in the tool answer
    — the loop's whole advantage over the blind retry."""
    stranded = {
        **GRAPH,
        "edges": [GRAPH["edges"][0]],  # `answer` never reaches `done` any more
    }
    model = ScriptedModel([_call("de_xuat_graph", {"workflow": stranded}), _ket_thuc("Còn dở.")])
    reply = _run(model, request())
    tool_answers = [m for m in model.seen[1] if isinstance(m, ToolMessage)]
    assert tool_answers, "the validator's verdict never reached the model"
    assert "phàn nàn" in tool_answers[0].content
    assert reply.van_de != ()


def test_malformed_proposal_is_a_sentence_not_a_crash() -> None:
    model = ScriptedModel([_call("de_xuat_graph", {"workflow": {"nonsense": True}}), _ket_thuc("Thôi.")])
    reply = _run(model, request())
    assert reply.workflow is None
    assert not reply.cac_buoc[0].ok


def test_budget_concludes_a_looping_model() -> None:
    """Six spent tool calls force a conclusion carrying the best standing proposal."""
    proposals = [_call("de_xuat_graph", {"workflow": GRAPH}, call_id=f"c{i}") for i in range(MAX_TOOL_CALLS + 2)]
    reply = _run(ScriptedModel(proposals), request())
    assert "trần" in reply.loi_nhan
    assert reply.workflow is not None
    assert len(reply.cac_buoc) == MAX_TOOL_CALLS


def test_xem_run_wraps_transcript_as_untrusted_data() -> None:
    model = ScriptedModel([_call("xem_run", {"ma_run": "7"}), _ket_thuc("Bước 2 chỉ đọc 1 nguồn.")])
    reply = _run(
        model,
        request(viewing_run_id="7", run_fetcher=lambda ma_run: "step search: found 1 document" if ma_run == "7" else None),
    )
    tool_answers = [m for m in model.seen[1] if isinstance(m, ToolMessage)]
    assert "untrusted" in tool_answers[0].content
    assert "<transcript>" in tool_answers[0].content
    assert reply.cac_buoc[0].tool == "xem_run"


def test_unknown_run_is_a_sentence() -> None:
    model = ScriptedModel([_call("xem_run", {"ma_run": "999"}), _ket_thuc()])
    _run(model, request(run_fetcher=lambda _ma_run: None))
    tool_answers = [m for m in model.seen[1] if isinstance(m, ToolMessage)]
    assert "Không tìm thấy" in tool_answers[0].content


def test_history_reaches_the_model() -> None:
    model = ScriptedModel([_ket_thuc()])
    _run(
        model,
        request(
            history=(
                HistoryTurn(role="creator", message="thêm bước duyệt"),
                HistoryTurn(role="meta_agent", message="Đã thêm."),
            )
        ),
    )
    sent = [getattr(m, "content", "") for m in model.seen[0]]
    assert any("thêm bước duyệt" in str(content) for content in sent)
