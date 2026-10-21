"""The composer's shape rules — each one a failure measured on 2026-08-30 (ADR-0067).

`structural_problems` says whether the runtime could execute a graph; these say
whether the graph does what the spec asked. Every rule here was the dominant
failure of a real composition run in `benchmark/pitch_kinh_doanh/results/`, so
each test reproduces the exact wrong shape the model drew and asserts the
sentence it is now told.
"""

import re

from src.metaagent.loop import CatalogAction, CatalogTool
from src.models.node_registry import NodeType
from src.models.solution_spec import AgentSolutionSpecification
from src.models.workflow import Edge, NodeInstance, WorkflowJSON
from src.workflow.composition import _repaired, _shape_complaints, offered_tools
from src.workflow.fill import connected_tool_ids


def _spec(**overrides: object) -> AgentSolutionSpecification:
    base = {
        "goal": "Trả lời câu hỏi về chính sách từ tài liệu đã tải lên",
        "users": ["Nhân viên kinh doanh"],
        "data": [{"name": "Bộ chính sách", "kind": "uploaded_file", "ref": "1"}],
        "actions": ["Tra chính sách", "Trả lời có trích điều khoản"],
        "expected_outcome": ["Một câu trả lời kèm điều khoản"],
        "success_criteria": [{"text": "Trả lời bám tài liệu", "category": "source_groundedness"}],
        "prohibitions": ["Không suy đoán"],
    }
    return AgentSolutionSpecification.model_validate({**base, **overrides})


def _node(node_id: str, node_type: NodeType, **config: object) -> NodeInstance:
    return NodeInstance(id=node_id, type=node_type, config=config)


def _edge(source: str, port: str, target: str) -> Edge:
    return Edge(id=f"{source}-{port}-{target}", source_node_id=source, source_port=port, target_node_id=target)


def _graph(nodes: list[NodeInstance], edges: list[Edge], start: str) -> WorkflowJSON:
    return WorkflowJSON(start_node_id=start, nodes=nodes, edges=edges)


GMAIL = CatalogTool(
    id="gmail",
    name="Gmail",
    actions=(
        CatalogAction(name="search_emails", description="tìm thư", read_only=True, risk="low", input_schema={"type": "object", "properties": {"query": {}}, "required": ["query"]}),
        CatalogAction(name="create_draft", description="lưu nháp", read_only=False, risk="medium", input_schema={"type": "object", "properties": {"to": {}, "subject": {}, "body": {}}}),
        CatalogAction(name="send_email", description="gửi thư", read_only=False, risk="high", input_schema={"type": "object", "properties": {"to": {}, "subject": {}, "body": {}}, "required": ["to", "subject", "body"]}),
    ),
)
ERP = CatalogTool(id="registered-1", name="ERP Kinh doanh", actions=(CatalogAction(name="cancel_order", description="huỷ đơn", read_only=False, risk="high", input_schema={"type": "object", "properties": {"order_id": {}}, "required": ["order_id"]}),))


# --------------------------------------------------------------------------- #
# What the composer is offered
# --------------------------------------------------------------------------- #


def test_a_spec_with_no_connected_account_is_offered_nothing() -> None:
    assert offered_tools(_spec(), (GMAIL, ERP)) == ()


def test_only_the_connection_the_spec_names_is_offered_and_by_its_whole_name() -> None:
    """*"Gmail của phòng Kinh doanh"* names Gmail — and does not name "ERP Kinh doanh" just because both say Kinh doanh."""
    spec = _spec(
        data=[{"name": "Hộp thư Gmail của phòng Kinh doanh", "kind": "connected_account"}],
        actions=["Soạn thư nhắc nợ", "Gửi thư sau khi duyệt"],
    )
    assert [tool.id for tool in offered_tools(spec, (GMAIL, ERP))] == ["gmail"]
    assert connected_tool_ids(spec.data, [("gmail", "Gmail"), ("registered-1", "ERP Kinh doanh")]) == ["gmail"]


def test_a_write_action_the_spec_never_talks_about_is_left_off_the_menu() -> None:
    """The spec says *gửi thư*, never *nháp* — so `create_draft` is not offered and cannot be drawn as a "prepare" step."""
    spec = _spec(
        data=[{"name": "Hộp thư Gmail của phòng", "kind": "connected_account"}],
        actions=["Soạn thư nhắc nợ", "Gửi thư sau khi duyệt"],
    )
    (gmail,) = offered_tools(spec, (GMAIL,))
    assert [action.name for action in gmail.actions] == ["send_email"]  # and no inbox read the spec never asked for


def test_a_filter_that_would_empty_the_menu_offers_everything_instead() -> None:
    """A spec whose words match no write action at all still gets the whole connection — the filter has guessed wrong."""
    spec = _spec(
        goal="Xử lý yêu cầu của khách qua kênh đã kết nối",
        data=[{"name": "Hộp Gmail của phòng", "kind": "connected_account"}],
        actions=["Xử lý yêu cầu của khách"],
        expected_outcome=["Yêu cầu được xử lý"],
        success_criteria=[{"text": "Xử lý đúng yêu cầu", "category": "correctness"}],
        prohibitions=["Không bịa"],
    )
    (gmail,) = offered_tools(spec, (GMAIL,))
    assert len(gmail.actions) == 3


# --------------------------------------------------------------------------- #
# Shapes that are refused
# --------------------------------------------------------------------------- #


def test_mcp_and_collect_are_refused_when_the_spec_names_no_account() -> None:
    graph = _graph(
        [
            _node("collect_q", NodeType.COLLECT, action_node="mcp_read"),
            _node("mcp_read", NodeType.MCP, tool_id="", action=""),
            _node("search", NodeType.KNOWLEDGE_SEARCH, source_ids=["1"]),
            _node("answer", NodeType.AGENT_LLM, prompt="trả lời", model="gpt-4.1-mini"),
            _node("end", NodeType.END),
        ],
        [_edge("collect_q", "complete", "mcp_read"), _edge("collect_q", "incomplete", "end"), _edge("mcp_read", "done", "search"), _edge("search", "found", "answer"), _edge("answer", "done", "end")],
        "collect_q",
    )
    complaints = _shape_complaints(graph, _spec(), frozenset())
    assert any("không được có node mcp hay collect" in c and "collect_q, mcp_read" in c for c in complaints)


def test_a_condition_written_as_code_is_refused() -> None:
    for condition in ("knowledge_search_policy.found == true", "Kết quả node check_complete là 'Đủ'", "output.bac != null"):
        graph = _graph(
            [
                _node("search", NodeType.KNOWLEDGE_SEARCH, source_ids=["1"]),
                _node("gate", NodeType.IF_ELSE, condition=condition),
                _node("a", NodeType.AGENT_LLM, prompt="x", model="gpt-4.1-mini"),
                _node("b", NodeType.AGENT_LLM, prompt="y", model="gpt-4.1-mini"),
                _node("end", NodeType.END),
            ],
            [_edge("search", "found", "gate"), _edge("gate", "true", "a"), _edge("gate", "false", "b"), _edge("a", "done", "end"), _edge("b", "done", "end")],
            "search",
        )
        assert any("viết kiểu mã" in c for c in _shape_complaints(graph, _spec(), frozenset())), condition


def test_a_business_condition_is_not_mistaken_for_code() -> None:
    graph = _graph(
        [
            _node("search", NodeType.KNOWLEDGE_SEARCH, source_ids=["1"]),
            _node("gate", NodeType.IF_ELSE, condition="Khoản nợ đã quá hạn từ 60 ngày trở lên, tức thuộc bậc cảnh báo"),
            _node("a", NodeType.AGENT_LLM, prompt="x", model="gpt-4.1-mini"),
            _node("b", NodeType.AGENT_LLM, prompt="y", model="gpt-4.1-mini"),
            _node("end", NodeType.END),
        ],
        [_edge("search", "found", "gate"), _edge("gate", "true", "a"), _edge("gate", "false", "b"), _edge("a", "done", "end"), _edge("b", "done", "end")],
        "search",
    )
    assert not any("viết kiểu mã" in c for c in _shape_complaints(graph, _spec(), frozenset()))


def _approval_graph(*, rejected_to: str, with_collect: bool = True, agent_writes: list[str] | None = None) -> WorkflowJSON:
    nodes = [
        _node("draft", NodeType.AGENT_LLM, prompt="soạn thư", model="gpt-4.1-mini"),
        _node("approve", NodeType.HUMAN_APPROVAL, approver_role="manager", message_template="Gửi không?"),
        _node("send", NodeType.MCP, tool_id="gmail", action=""),
        _node("told", NodeType.AGENT_LLM, prompt="kể lại", model="gpt-4.1-mini"),
        _node("refused", NodeType.AGENT_LLM, prompt="chưa gửi", model="gpt-4.1-mini"),
        _node("end", NodeType.END),
    ]
    edges = [
        _edge("draft", "done", "approve"),
        _edge("approve", "approved", "send"),
        _edge("approve", "rejected", rejected_to),
        _edge("send", "done", "told"),
        _edge("told", "done", "end"),
        _edge("refused", "done", "end"),
    ]
    start = "draft"
    if with_collect:
        nodes.insert(0, _node("ask", NodeType.COLLECT, action_node="send", agent_writes=agent_writes if agent_writes is not None else ["subject", "body"]))
        edges = [_edge("ask", "complete", "draft"), _edge("ask", "incomplete", "end"), *edges]
        start = "ask"
    return _graph(nodes, edges, start)


MAIL_SPEC = dict(
    goal="Gửi thư nhắc nợ sau khi trưởng nhóm duyệt",
    data=[{"name": "Hộp thư Gmail của phòng", "kind": "connected_account"}, {"name": "Thông tin khoản nợ", "kind": "user_input"}],
    actions=["Soạn thư", "Gửi thư qua Gmail sau khi duyệt"],
)
MAIL_PARAMS = frozenset({"to", "subject", "body", "query"})


def test_rejected_wired_straight_to_end_is_refused() -> None:
    complaints = _shape_complaints(_approval_graph(rejected_to="end"), _spec(**MAIL_SPEC), MAIL_PARAMS, frozenset({"to"}))
    assert any("Cổng rejected" in c for c in complaints)
    assert not any("Cổng rejected" in c for c in _shape_complaints(_approval_graph(rejected_to="refused"), _spec(**MAIL_SPEC), MAIL_PARAMS, frozenset({"to"})))


def test_agent_writes_is_split_by_rule_not_by_asking_the_model_again() -> None:
    """`to` comes out (the user decides it), `body` goes in (the agent writes it) — for free, before judging.

    Round 4 of the 2026-08-30 measurement: the only complaint left standing on the
    third attempt of both 13-node graphs was an empty `agent_writes`, and the
    fallback that followed threw away an otherwise correct graph.
    """
    from src.workflow.composition import _composed_writes

    def writes_of(graph: WorkflowJSON) -> list[str]:
        (ask,) = [node for node in graph.nodes if node.type is NodeType.COLLECT]
        return list(ask.config["agent_writes"])

    assert writes_of(_composed_writes(_approval_graph(rejected_to="refused", agent_writes=[]), MAIL_PARAMS)) == ["body"]
    assert writes_of(_composed_writes(_approval_graph(rejected_to="refused", agent_writes=["to", "subject", "body"]), MAIL_PARAMS)) == ["subject", "body"]
    untouched = _approval_graph(rejected_to="refused", agent_writes=["subject", "body"])
    assert _composed_writes(untouched, MAIL_PARAMS) == untouched
    # No composed parameter on offer (a read-only connection): nothing is invented.
    assert writes_of(_composed_writes(_approval_graph(rejected_to="refused", agent_writes=[]), frozenset({"query"}))) == []


def test_an_action_that_needs_the_users_address_needs_a_collector() -> None:
    graph = _approval_graph(rejected_to="refused", with_collect=False)
    assert any("cần một node collect" in c for c in _shape_complaints(graph, _spec(**MAIL_SPEC), MAIL_PARAMS, frozenset({"to"})))


def test_a_stated_threshold_without_an_if_else_is_refused() -> None:
    spec = _spec(**MAIL_SPEC, success_criteria=[{"text": "Quá hạn từ 60 ngày trở lên là thư cảnh báo", "category": "source_groundedness"}])
    complaints = _shape_complaints(_approval_graph(rejected_to="refused"), spec, MAIL_PARAMS, frozenset({"to"}))
    assert any("từ 60 ngày" in c and "if_else" in c for c in complaints)


def test_the_spec_s_sources_must_each_have_their_node_and_no_more() -> None:
    web_only = _graph(
        [_node("web", NodeType.WEB_SEARCH), _node("answer", NodeType.AGENT_LLM, prompt="x", model="gpt-4.1-mini"), _node("end", NodeType.END)],
        [_edge("web", "done", "answer"), _edge("answer", "done", "end")],
        "web",
    )
    complaints = _shape_complaints(web_only, _spec(), frozenset())
    assert any("không có node knowledge_search" in c for c in complaints)
    assert any("bỏ node web_search" in c for c in complaints)


def test_nothing_that_acts_or_stops_may_live_inside_a_while_body() -> None:
    graph = _graph(
        [
            _node("search", NodeType.KNOWLEDGE_SEARCH, source_ids=["1"]),
            _node("loop", NodeType.WHILE, condition="bản thảo còn thiếu mục", max_iterations=3),
            _node("draft", NodeType.AGENT_LLM, prompt="soạn", model="gpt-4.1-mini"),
            _node("ask", NodeType.COLLECT, action_node="save", agent_writes=["content"]),
            _node("save", NodeType.MCP, tool_id="google_drive", action=""),
            _node("told", NodeType.AGENT_LLM, prompt="kể", model="gpt-4.1-mini"),
            _node("end", NodeType.END),
        ],
        [
            _edge("search", "found", "loop"),
            _edge("loop", "loop", "draft"),
            _edge("draft", "done", "ask"),
            _edge("ask", "complete", "save"),
            _edge("ask", "incomplete", "end"),
            _edge("save", "done", "loop"),
            _edge("loop", "exit", "told"),
            _edge("told", "done", "end"),
        ],
        "search",
    )
    spec = _spec(data=[{"name": "Mẫu biên bản", "kind": "uploaded_file", "ref": "1"}, {"name": "Google Drive của phòng", "kind": "connected_account"}], actions=["Soạn biên bản", "Lưu thành Google Doc trong Drive"])
    complaints = _shape_complaints(graph, spec, frozenset({"title", "content"}), frozenset({"title"}))
    assert any("Thân vòng lặp" in c and "ask" in c and "save" in c for c in complaints)


def test_an_mcp_node_named_after_an_unoffered_write_action_is_refused() -> None:
    """The spec said *gửi*, never *nháp* — a node the model calls `mcp_create_draft` is the habit rule G names."""
    graph = _approval_graph(rejected_to="refused")
    draft_step = _node("mcp_create_draft", NodeType.MCP, tool_id="gmail", action="")
    with_draft = graph.model_copy(update={"nodes": [*graph.nodes, draft_step]})
    complaints = _shape_complaints(with_draft, _spec(**MAIL_SPEC), MAIL_PARAMS, frozenset({"to"}), frozenset({"draft"}))
    assert any("mcp_create_draft" in c and "['draft']" in c for c in complaints)
    assert not any("nhắm tới action" in c for c in _shape_complaints(graph, _spec(**MAIL_SPEC), MAIL_PARAMS, frozenset({"to"}), frozenset({"draft"})))


def test_an_agent_llm_feeding_a_read_node_is_refused() -> None:
    """web → summarise → knowledge_search (2026-08-30, run 9): the search never reads what the summariser wrote."""
    graph = _graph(
        [
            _node("web", NodeType.WEB_SEARCH),
            _node("sum", NodeType.AGENT_LLM, prompt="tóm tắt web", model="gpt-4.1-mini"),
            _node("search", NodeType.KNOWLEDGE_SEARCH, source_ids=["1"]),
            _node("answer", NodeType.AGENT_LLM, prompt="trả lời", model="gpt-4.1-mini"),
            _node("end", NodeType.END),
        ],
        [_edge("web", "done", "sum"), _edge("sum", "done", "search"), _edge("search", "found", "answer"), _edge("answer", "done", "end")],
        "web",
    )
    spec = _spec(data=[{"name": "Thông tin khách trên web", "kind": "web"}, {"name": "Chính sách", "kind": "uploaded_file", "ref": "1"}])
    assert any("nối vào node knowledge_search" in c for c in _shape_complaints(graph, spec, frozenset()))


def test_an_extractor_in_front_of_a_web_search_is_accepted() -> None:
    """The shape ADR-0068 asks the composer for: extract the company name, then search for that.

    Two things had to stop refusing it — rule H (an `agent_llm` may not feed a read node) and rule J
    (a read-and-answer graph has one `agent_llm`). Both are asserted here, because relaxing either
    alone leaves the composer redrawing the shape and being told no.
    """
    graph = _graph(
        [
            _node("extract", NodeType.AGENT_LLM, prompt="in ra tên công ty khách", model="gpt-4.1-mini"),
            _node("web", NodeType.WEB_SEARCH),
            _node("search", NodeType.KNOWLEDGE_SEARCH, source_ids=["1"]),
            _node("answer", NodeType.AGENT_LLM, prompt="trả lời", model="gpt-4.1-mini"),
            _node("end", NodeType.END),
        ],
        [
            _edge("extract", "done", "web"),
            _edge("web", "done", "search"),
            _edge("search", "found", "answer"),
            _edge("answer", "done", "end"),
        ],
        "extract",
    )
    spec = _spec(data=[{"name": "Thông tin khách trên web", "kind": "web"}, {"name": "Chính sách", "kind": "uploaded_file", "ref": "1"}])
    complaints = _shape_complaints(graph, spec, frozenset())
    assert not any("nối vào node knowledge_search" in c for c in complaints)
    assert not any("node agent_llm" in c for c in complaints)


def test_two_answering_nodes_are_still_refused_when_one_extractor_is_present() -> None:
    """The extractor is exempt from rule J; a second *answering* node is not."""
    graph = _graph(
        [
            _node("extract", NodeType.AGENT_LLM, prompt="in ra tên công ty khách", model="gpt-4.1-mini"),
            _node("web", NodeType.WEB_SEARCH),
            _node("sum", NodeType.AGENT_LLM, prompt="tóm tắt web", model="gpt-4.1-mini"),
            _node("answer", NodeType.AGENT_LLM, prompt="trả lời", model="gpt-4.1-mini"),
            _node("end", NodeType.END),
        ],
        [
            _edge("extract", "done", "web"),
            _edge("web", "done", "sum"),
            _edge("sum", "done", "answer"),
            _edge("answer", "done", "end"),
        ],
        "extract",
    )
    spec = _spec(data=[{"name": "Thông tin khách trên web", "kind": "web"}])
    complaints = _shape_complaints(graph, spec, frozenset())
    assert any("node agent_llm" in c and "extract" not in c for c in complaints)


def test_more_read_nodes_than_sources_is_refused() -> None:
    """Three `web_search` nodes for one web source (2026-08-30, run 7) is three paid searches for one answer."""
    graph = _graph(
        [
            _node("w1", NodeType.WEB_SEARCH),
            _node("w2", NodeType.WEB_SEARCH),
            _node("w3", NodeType.WEB_SEARCH),
            _node("search", NodeType.KNOWLEDGE_SEARCH, source_ids=["1"]),
            _node("answer", NodeType.AGENT_LLM, prompt="x", model="gpt-4.1-mini"),
            _node("end", NodeType.END),
        ],
        [_edge("w1", "done", "w2"), _edge("w2", "done", "w3"), _edge("w3", "done", "search"), _edge("search", "found", "answer"), _edge("answer", "done", "end")],
        "w1",
    )
    spec = _spec(data=[{"name": "Thông tin khách trên web", "kind": "web"}, {"name": "Chính sách", "kind": "uploaded_file", "ref": "1"}])
    assert any("3 node web_search cho 1 nguồn web" in c for c in _shape_complaints(graph, spec, frozenset()))


def test_a_collector_whose_incomplete_port_was_forgotten_is_wired_to_end_for_free() -> None:
    from src.workflow.composition import _wired_collectors

    graph = _graph(
        [
            _node("ask", NodeType.COLLECT, action_node="save", agent_writes=["content"]),
            _node("save", NodeType.MCP, tool_id="google_drive", action=""),
            _node("told", NodeType.AGENT_LLM, prompt="kể", model="gpt-4.1-mini"),
            _node("end", NodeType.END),
        ],
        [_edge("ask", "complete", "save"), _edge("save", "done", "told"), _edge("told", "done", "end")],
        "ask",
    )
    wired = _wired_collectors(graph)
    incomplete = [edge for edge in wired.edges if edge.source_node_id == "ask" and edge.source_port == "incomplete"]
    assert [edge.target_node_id for edge in incomplete] == ["end"]
    assert _wired_collectors(wired) == wired  # already wired: untouched


def test_built_in_reads_are_offered_only_when_the_spec_reads_that_account() -> None:
    """*Gửi thư* offers `send_email` and no inbox read; *đọc thư khách gửi* brings `read_email` back."""
    sending = _spec(
        data=[{"name": "Hộp thư Gmail của phòng", "kind": "connected_account"}],
        actions=["Soạn thư nhắc nợ", "Gửi thư sau khi duyệt"],
    )
    (gmail,) = offered_tools(sending, (GMAIL,))
    assert [action.name for action in gmail.actions] == ["send_email"]

    reading = _spec(
        data=[{"name": "Hộp thư Gmail của phòng", "kind": "connected_account"}],
        actions=["Đọc thư khách gửi tới", "Gửi thư trả lời sau khi duyệt"],
    )
    (gmail,) = offered_tools(reading, (GMAIL,))
    assert "search_emails" in {action.name for action in gmail.actions} or "send_email" in {action.name for action in gmail.actions}


def test_more_mcp_nodes_than_offered_actions_is_refused() -> None:
    graph = _approval_graph(rejected_to="refused")
    extra = _node("mcp_read_policy", NodeType.MCP, tool_id="gmail", action="")
    with_extra = graph.model_copy(update={"nodes": [*graph.nodes, extra]})
    complaints = _shape_complaints(with_extra, _spec(**MAIL_SPEC), MAIL_PARAMS, frozenset({"to"}), frozenset(), ("gmail.send_email",))
    assert any("2 node mcp" in c and "1 action" in c for c in complaints)
    assert not any("node mcp (" in c for c in _shape_complaints(graph, _spec(**MAIL_SPEC), MAIL_PARAMS, frozenset({"to"}), frozenset(), ("gmail.send_email",)))


def test_a_chain_of_read_nodes_beyond_the_sources_is_collapsed_for_free() -> None:
    from src.workflow.composition import _deduplicated_reads

    graph = _graph(
        [
            _node("w1", NodeType.WEB_SEARCH, query_hint="ngành nghề"),
            _node("w2", NodeType.WEB_SEARCH, query_hint="quy mô"),
            _node("w3", NodeType.WEB_SEARCH, query_hint="tin gần đây"),
            _node("search", NodeType.KNOWLEDGE_SEARCH, source_ids=["1"]),
            _node("answer", NodeType.AGENT_LLM, prompt="x", model="gpt-4.1-mini"),
            _node("end", NodeType.END),
        ],
        [_edge("w1", "done", "w2"), _edge("w2", "done", "w3"), _edge("w3", "done", "search"), _edge("search", "found", "answer"), _edge("answer", "done", "end")],
        "w1",
    )
    spec = _spec(data=[{"name": "Thông tin khách trên web", "kind": "web"}, {"name": "Chính sách", "kind": "uploaded_file", "ref": "1"}])
    collapsed = _deduplicated_reads(graph, spec)
    assert [node.id for node in collapsed.nodes] == ["w1", "search", "answer", "end"]
    assert [(e.source_node_id, e.target_node_id) for e in collapsed.edges] == [("search", "answer"), ("answer", "end"), ("w1", "search")]
    assert _deduplicated_reads(collapsed, spec) == collapsed


def test_nhap_is_not_nhap_and_nhac_is_neither() -> None:
    """*do nhân viên nhập* (enter) and *thư nhắc nợ* (remind) must not offer `create_draft` — only *nháp* does."""
    entering = _spec(
        data=[{"name": "Hộp thư Gmail của phòng", "kind": "connected_account"}],
        actions=["Nhận thông tin do nhân viên nhập", "Soạn thư nhắc nợ", "Gửi thư sau khi duyệt"],
    )
    (gmail,) = offered_tools(entering, (GMAIL,))
    assert [action.name for action in gmail.actions] == ["send_email"]
    drafting = _spec(
        data=[{"name": "Hộp thư Gmail của phòng", "kind": "connected_account"}],
        actions=["Soạn thư báo giá", "Để lại bản nháp trong Gmail"],
    )
    (gmail,) = offered_tools(drafting, (GMAIL,))
    assert "create_draft" in {action.name for action in gmail.actions}


def test_a_missing_collector_is_inserted_in_front_of_the_action_for_free() -> None:
    from src.workflow.composition import _collected

    graph = _approval_graph(rejected_to="refused", with_collect=False)
    repaired = _collected(graph, frozenset({"to"}))
    (ask,) = [node for node in repaired.nodes if node.type is NodeType.COLLECT]
    assert ask.config["action_node"] == "send"
    ports = {(e.source_port, e.target_node_id) for e in repaired.edges if e.source_node_id == ask.id}
    assert ports == {("complete", "send"), ("incomplete", "end")}
    assert [e.target_node_id for e in repaired.edges if e.source_node_id == "approve" and e.source_port == "approved"] == [ask.id]
    assert _collected(repaired, frozenset({"to"})) == repaired
    assert _collected(graph, frozenset()) == graph  # no named connection: nothing to insert


def test_a_read_and_answer_graph_has_one_answering_node() -> None:
    graph = _graph(
        [
            _node("web", NodeType.WEB_SEARCH),
            _node("search", NodeType.KNOWLEDGE_SEARCH, source_ids=["1"]),
            _node("sum_web", NodeType.AGENT_LLM, prompt="a", model="gpt-4.1-mini"),
            _node("sum_policy", NodeType.AGENT_LLM, prompt="b", model="gpt-4.1-mini"),
            _node("final", NodeType.AGENT_LLM, prompt="c", model="gpt-4.1-mini"),
            _node("end", NodeType.END),
        ],
        [_edge("web", "done", "search"), _edge("search", "found", "sum_web"), _edge("sum_web", "done", "sum_policy"), _edge("sum_policy", "done", "final"), _edge("final", "done", "end")],
        "web",
    )
    spec = _spec(data=[{"name": "Thông tin khách trên web", "kind": "web"}, {"name": "Chính sách", "kind": "uploaded_file", "ref": "1"}])
    assert any("3 node agent_llm" in c for c in _shape_complaints(graph, spec, frozenset()))


def test_a_named_connection_with_an_action_to_call_needs_an_mcp_node() -> None:
    """The interview kept Gmail in the spec, the composer drew a graph that never sends (2026-08-30, runs 23–24)."""
    graph = _graph(
        [
            _node("search", NodeType.KNOWLEDGE_SEARCH, source_ids=["1"]),
            _node("draft", NodeType.AGENT_LLM, prompt="soạn thư", model="gpt-4.1-mini"),
            _node("end", NodeType.END),
        ],
        [_edge("search", "found", "draft"), _edge("draft", "done", "end")],
        "search",
    )
    complaints = _shape_complaints(graph, _spec(**MAIL_SPEC), MAIL_PARAMS, frozenset({"to"}), frozenset(), ("gmail.send_email",))
    assert any("không có node mcp nào" in c for c in complaints)


def test_a_condition_threshold_must_be_the_specs_threshold() -> None:
    """The spec says 60 ngày; a condition saying 30 ngày (2026-08-30, runs 28–29) is refused, 60 is not."""
    spec = _spec(**MAIL_SPEC, success_criteria=[{"text": "Quá hạn từ 60 ngày trở lên là thư cảnh báo", "category": "source_groundedness"}])

    def with_condition(condition: str) -> WorkflowJSON:
        graph = _approval_graph(rejected_to="refused")
        gate = _node("gate", NodeType.IF_ELSE, condition=condition)
        other = _node("soft", NodeType.AGENT_LLM, prompt="thư nhẹ", model="gpt-4.1-mini")
        return graph.model_copy(
            update={
                "nodes": [*graph.nodes, gate, other],
                "edges": [*graph.edges, _edge("gate", "true", "draft"), _edge("gate", "false", "soft"), _edge("soft", "done", "end")],
            }
        )

    wrong = _shape_complaints(with_condition("Khoản nợ đã quá hạn từ 30 ngày trở lên"), spec, MAIL_PARAMS, frozenset({"to"}))
    assert any("30 ngày" in c and "60 ngày" in c for c in wrong)
    right = _shape_complaints(with_condition("Khoản nợ đã quá hạn từ 60 ngày trở lên"), spec, MAIL_PARAMS, frozenset({"to"}))
    assert not any("không có trong bài toán" in c for c in right)


def test_a_spec_with_no_number_at_all_still_refuses_an_invented_threshold() -> None:
    """The case rule K used to skip (2026-08-31, runs 58–59): no quantity in the spec, "30 ngày" in a condition.

    `if spec_numbers:` guarded the whole loop, so a spec that stated no threshold — exactly the spec that
    cannot justify one — was the only spec a model could put any number it liked into.
    """
    spec = _spec(**MAIL_SPEC)  # not one quantity in goal/actions/expected_outcome/criteria/prohibitions
    assert not re.search(r"\d", " ".join([spec.goal, *spec.actions, *spec.prohibitions]))

    def with_condition(condition: str) -> WorkflowJSON:
        graph = _approval_graph(rejected_to="refused")
        gate = _node("gate", NodeType.IF_ELSE, condition=condition)
        other = _node("soft", NodeType.AGENT_LLM, prompt="thư nhẹ", model="gpt-4.1-mini")
        return graph.model_copy(
            update={
                "nodes": [*graph.nodes, gate, other],
                "edges": [*graph.edges, _edge("gate", "true", "draft"), _edge("gate", "false", "soft"), _edge("soft", "done", "end")],
            }
        )

    invented = _shape_complaints(with_condition("Khoản nợ đã quá hạn từ 30 ngày trở lên"), spec, MAIL_PARAMS, frozenset({"to"}))
    assert any("30 ngày" in c and "KHÔNG nêu một con số nào" in c for c in invented)

    # A condition that judges the same thing without inventing a number is still fine.
    worded = _shape_complaints(with_condition("Khoản nợ đã quá hạn tới mức chính sách gọi là bậc cảnh báo"), spec, MAIL_PARAMS, frozenset({"to"}))
    assert not any("ngưỡng" in c and "gate" in c for c in worded)


def test_on_a_graph_with_no_approval_a_collector_is_moved_in_front_of_its_action() -> None:
    """collect → search → while ⇄ draft → mcp (2026-08-30, run 36) becomes search → while ⇄ draft → collect → mcp."""
    from src.workflow.composition import _relocated_collectors, _wired_collectors

    graph = _graph(
        [
            _node("ask", NodeType.COLLECT, action_node="save", agent_writes=["content"]),
            _node("search", NodeType.KNOWLEDGE_SEARCH, source_ids=["1"]),
            _node("loop", NodeType.WHILE, condition="bản thảo còn thiếu mục", max_iterations=3),
            _node("draft", NodeType.AGENT_LLM, prompt="soạn", model="gpt-4.1-mini"),
            _node("save", NodeType.MCP, tool_id="google_drive", action=""),
            _node("told", NodeType.AGENT_LLM, prompt="kể", model="gpt-4.1-mini"),
            _node("end", NodeType.END),
        ],
        [
            _edge("ask", "complete", "search"),
            _edge("ask", "incomplete", "end"),
            _edge("search", "found", "loop"),
            _edge("loop", "loop", "draft"),
            _edge("draft", "done", "loop"),
            _edge("loop", "exit", "save"),
            _edge("save", "done", "told"),
            _edge("told", "done", "end"),
        ],
        "ask",
    )
    moved = _wired_collectors(_relocated_collectors(graph))
    assert moved.start_node_id == "search"
    leaving = {(e.source_node_id, e.source_port): e.target_node_id for e in moved.edges}
    assert leaving[("loop", "exit")] == "ask"
    assert leaving[("ask", "complete")] == "save"
    assert leaving[("ask", "incomplete")] == "end"
    assert ("ask", "complete") in leaving and leaving[("search", "found")] == "loop"
    # With an approval on the graph the collector stays where the composer put it.
    with_approval = _approval_graph(rejected_to="refused")
    assert _relocated_collectors(with_approval) == with_approval


def test_an_end_node_written_without_its_type_is_repaired_not_rejected() -> None:
    document = {"start_node_id": "a", "nodes": [{"id": "a", "type": "agent_llm", "config": {"prompt": "x"}}, {"id": "end"}], "edges": []}
    repaired = _repaired(document)
    assert repaired["nodes"][1] == {"id": "end", "type": "end", "config": {}}
    WorkflowJSON.model_validate(repaired)
