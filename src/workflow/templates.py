"""The catalog the Workflow Generator chooses from — ADR-0025.

Requirement §2 rules out a free-form LLM-drawn workflow: the Generator picks a
template and configures it. A template is therefore a **static graph skeleton** —
a fixed set of nodes wired a fixed way, with `config` holding placeholders —
never a builder that varies its shape with the spec. That is what keeps
`WorkflowJSON.metadata.template_id` worth recording: the version-compare
dashboard can only tell "same template, different config" from "different
template entirely" if the shape an id names is actually constant.

One tier, not two (ADR-0025 §2): each use case is its own entry even when it
duplicates another's topology. The registry is code and the team edits it, so
the cost of the duplication is a few lines here rather than repeated effort by
anyone using the platform.

**Two entries, not the seven ADR-0025 §9 settled on.** These are the two the
demo needs — `document_qa` and `action_with_approval`, matching requirement §2's
worked example literally — and they are the two that prove both halves of the
selection rule: one needs a data source, the other needs a person standing in
front of a side effect. The other five (`planning_helper`, `customer_service`,
`document_comparison`, `internal_knowledge_assistant`, `data_enrichment`) are
#54 and are
additions to this dict and nothing else.

Only `source: built_in` entries live here. ADR-0025 §6 keeps the registry hybrid
— creators submit templates from an already-confirmed workflow, and those are DB
rows at `status: pending` until an admin approves them, the same registry/DB
split `ToolDefinition` uses. `approved_templates` is written to be the one place
the two sources would be joined, so adding the DB half later changes one
function rather than every caller.
"""

from collections.abc import Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from src.models.node_registry import NodeType, is_prose_field, is_resource_field
from src.models.solution_spec import DataSourceKind
from src.models.workflow import Edge, NodeInstance, WorkflowJSON, WorkflowMetadata

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class TemplateSource(StrEnum):
    """Who wrote a template, which is what decides where it is stored.

    `built_in` entries are this module; `creator_submitted` ones are DB rows
    (ADR-0025 §6), and only ever arrive via "save as template" from a workflow
    that already passed the graph validator (§7). Both read as one shape.
    """

    BUILT_IN = "built_in"
    CREATOR_SUBMITTED = "creator_submitted"


class TemplateStatus(StrEnum):
    """Whether the Generator may propose this template.

    Only `approved` joins the candidate pool. `disabled` removes an entry from
    future selection while leaving every workflow already generated from it
    untouched — a produced workflow is a copied graph, not a live reference.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISABLED = "disabled"


#: How far apart nodes sit when a skeleton is first drawn. The canvas lays out
#: left-to-right (ADR-0027), and a template that arrived stacked on one point
#: would greet the creator as a pile they have to untangle before reading.
_COLUMN = 260
_ROW = 160

#: The approver a built-in template asks for when it has no creator behind it
#: yet. Kept rather than cleared on save-as-template (ADR-0025 §8) because a
#: role is the shape of the pattern, not one creator's resource.
_DEFAULT_APPROVER_ROLE = "manager"


def _at(column: int, row: int = 0) -> dict[str, float]:
    """Canvas coordinates for the node in `column`, `row` — left to right."""
    return {"x": float(column * _COLUMN), "y": float(row * _ROW)}


class TemplateSpec(BaseModel):
    """One use case, as a fixed graph with its configuration left blank.

    `description` is written for the Generator's LLM to read, not for a menu:
    it is the whole basis on which a business problem gets matched to a shape,
    so it names the *situation* the template fits rather than the nodes it
    contains — the LLM can already see the nodes.

    `required_data_kinds` is what the rule validator checks a proposal against
    (ADR-0025 §3): a template whose graph searches attached documents cannot do
    anything for a spec that attached none. An empty list means the template
    needs no data source at all, not that any will do.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    description: str = Field(..., description="Template này hợp với bài toán nào — LLM đọc chính câu này để chọn")
    nodes: list[NodeInstance]
    edges: list[Edge]
    start_node_id: str
    required_data_kinds: list[DataSourceKind] = Field(
        default_factory=list, description="Spec phải có ít nhất một nguồn dữ liệu thuộc các loại này"
    )
    source: TemplateSource = TemplateSource.BUILT_IN
    status: TemplateStatus = TemplateStatus.APPROVED

    def skeleton(self) -> WorkflowJSON:
        """This template as a `WorkflowJSON`, placeholders and all.

        A copy, not a reference: ADR-0025's Consequences make a produced
        workflow a literal graph rather than a live pointer back here, which is
        why disabling or editing a template never retroactively changes a
        workflow already generated from it — and why no template needs a version
        field.
        """
        return WorkflowJSON(
            start_node_id=self.start_node_id,
            nodes=list(self.nodes),
            edges=list(self.edges),
            metadata=WorkflowMetadata(template_id=self.id),
        )


def strip_for_submission(nodes: Sequence[NodeInstance]) -> list[NodeInstance]:
    """`nodes`, with resource-references and business prose cleared to placeholders — ADR-0025 §8.

    The inverse of what `src.workflow.fill` fills in, and it reuses the same
    field markers (`is_resource_field`, `is_prose_field`) rather than a second,
    hand-kept list of field names — two lists would eventually name a ninth
    field differently, and the way that shows up here is a creator's own file
    key or wording leaking into a template somebody else's spec gets built on.

    `source_ids`/`tool_id`/`action` point at the submitting creator's own files
    and connections; `prompt`/`condition`/`message_template` carry their own
    business wording. Both are cleared. Everything else — `While.max_iterations`,
    a `HumanApproval.approver_role` like `"manager"` — is a structural constant
    of the shape, not a fact about the creator who happened to submit it, and is
    kept verbatim as the new template's default.
    """
    return [_stripped(node) for node in nodes]


def _stripped(node: NodeInstance) -> NodeInstance:
    """One node, its resource and prose fields blanked to the type their default already is."""
    config = {
        name: (
            _placeholder_of(value) if is_resource_field(node.type, name) or is_prose_field(node.type, name) else value
        )
        for name, value in node.config.items()
    }
    return node.model_copy(update={"config": config})


def _placeholder_of(value: Any) -> Any:
    """The blank a stripped field goes back to — `[]` for a list, `""` for everything else.

    Every resource and prose field in `NODE_REGISTRY` is one of the two, so this
    is a property of the value's own shape rather than a table keyed by field
    name that would need its own upkeep as fields are added.
    """
    return [] if isinstance(value, list) else ""


def _node(node_id: str, node_type: NodeType, config: dict[str, Any], column: int, row: int = 0) -> NodeInstance:
    """One skeleton node, placed where the canvas will first draw it."""
    return NodeInstance.model_validate(
        {"id": node_id, "type": node_type.value, "config": config, "position": _at(column, row)}
    )


def _edge(source_node_id: str, source_port: str, target_node_id: str) -> Edge:
    """One skeleton edge, with an id naming both ends so a canvas delete is unambiguous."""
    return Edge(
        id=f"{source_node_id}:{source_port}->{target_node_id}",
        source_node_id=source_node_id,
        source_port=source_port,
        target_node_id=target_node_id,
    )


_DOCUMENT_QA = TemplateSpec(
    id="document_qa",
    name="Hỏi đáp trên tài liệu",
    description=(
        "Người dùng hỏi một câu, agent tìm trong tài liệu đã gắn rồi trả lời dựa trên đúng những gì tìm được. "
        "Hợp với trợ lý tra cứu quy định, sổ tay nghiệp vụ, tài liệu nội bộ — không thực hiện hành động nào ra "
        "bên ngoài."
    ),
    start_node_id="search",
    nodes=[
        _node("search", NodeType.KNOWLEDGE_SEARCH, {"source_ids": []}, column=0),
        _node("answer", NodeType.AGENT_LLM, {"prompt": "", "model": ""}, column=1),
        _node("finish", NodeType.END, {}, column=2),
    ],
    edges=[
        _edge("search", "found", "answer"),
        _edge("answer", "done", "finish"),
    ],
    # Only `uploaded_file` sources carry a Knowledge Base id the `search` node
    # can be pointed at (ADR-0043 §1) — a `connected_account`'s `ref` is a
    # connection id, never a shelf, and `web`/`user_input` have no `ref` at all.
    required_data_kinds=[DataSourceKind.UPLOADED_FILE],
)

_ACTION_WITH_APPROVAL = TemplateSpec(
    id="action_with_approval",
    name="Hành động có người duyệt",
    description=(
        "Agent tra tài liệu, xét xem trường hợp này có đủ điều kiện không, xin một người duyệt, rồi mới thực hiện "
        "hành động thật ra bên ngoài (gửi thư, cập nhật hệ thống) và báo lại kết quả. Hợp với mọi bài toán mà một "
        "bước có tác dụng phụ không được xảy ra trước khi có người xác nhận."
    ),
    start_node_id="search",
    nodes=[
        _node("search", NodeType.KNOWLEDGE_SEARCH, {"source_ids": []}, column=0),
        _node("assess", NodeType.AGENT_LLM, {"prompt": "", "model": ""}, column=1),
        _node("gate", NodeType.IF_ELSE, {"condition": ""}, column=2),
        _node(
            "approval",
            NodeType.HUMAN_APPROVAL,
            {"approver_role": _DEFAULT_APPROVER_ROLE, "message_template": ""},
            column=3,
        ),
        _node("act", NodeType.MCP, {"tool_id": "", "action": ""}, column=4),
        _node("report", NodeType.AGENT_LLM, {"prompt": "", "model": ""}, column=5, row=1),
        _node("finish", NodeType.END, {}, column=6, row=1),
    ],
    edges=[
        _edge("search", "found", "assess"),
        _edge("assess", "done", "gate"),
        _edge("gate", "true", "approval"),
        # The three ways this workflow ends without acting — not eligible, not
        # approved, or done acting — all rejoin at one node, because what the
        # user is owed in every case is the same: an account of what happened.
        _edge("gate", "false", "report"),
        _edge("approval", "approved", "act"),
        _edge("approval", "rejected", "report"),
        _edge("act", "done", "report"),
        _edge("report", "done", "finish"),
    ],
    # A connected account is the one thing this shape cannot do without: the
    # `MCP` node acts on somebody's real system, and there is no acting on a
    # system nobody connected.
    required_data_kinds=[DataSourceKind.CONNECTED_ACCOUNT],
)


_PLANNING_HELPER = TemplateSpec(
    id="planning_helper",
    name="Trợ lý lập kế hoạch",
    description=(
        "Người dùng đưa ra một mục tiêu hoặc một việc cần làm, agent phân tích rồi trả lời bằng một kế hoạch "
        "từng bước để đạt được mục tiêu đó. Hợp với trợ lý lập kế hoạch, chia nhỏ công việc, gợi ý lộ trình — "
        "không tra cứu tài liệu nào, không thực hiện hành động nào ra bên ngoài."
    ),
    start_node_id="plan",
    nodes=[
        _node("plan", NodeType.AGENT_LLM, {"prompt": "", "model": ""}, column=0),
        _node("finish", NodeType.END, {}, column=1),
    ],
    edges=[
        _edge("plan", "done", "finish"),
    ],
    # Nothing here to search and nothing to act on — the plan is written entirely
    # from the goal the creator already put into the spec, so no `required_data_kinds`
    # entry names a source this shape actually needs.
)

_DIRECT_ASSISTANT = TemplateSpec(
    id="direct_assistant",
    name="Trợ lý trả lời trực tiếp",
    description=(
        "Agent đọc yêu cầu của người dùng rồi trả lời thẳng bằng năng lực của model — phân loại, tóm tắt, viết lại, "
        "trích xuất thông tin, dịch, soạn nội dung, tư vấn. Không tra tài liệu đính kèm, không tìm trên web, không "
        "gọi dịch vụ bên ngoài. Đây là bộ khung chung nhất: hợp với mọi bài toán chỉ cần model suy nghĩ rồi trả lời, "
        "và là nơi bắt đầu tốt khi bài toán chưa rõ cần tra cứu gì."
    ),
    start_node_id="respond",
    nodes=[
        _node("respond", NodeType.AGENT_LLM, {"prompt": "", "model": ""}, column=0),
        _node("finish", NodeType.END, {}, column=1),
    ],
    edges=[
        _edge("respond", "done", "finish"),
    ],
    # No `required_data_kinds`, and that is what makes this the shape `_closest`
    # falls back to: it presumes nothing about where the answer comes from, so it
    # is the honest thing to hand somebody when the platform could not work out
    # what they are building. The same graph as `_PLANNING_HELPER` deliberately —
    # what differs is the `description`, which is the only thing the selector
    # matches on, and "make me a plan" is a narrower ask than "answer this".
)

_CUSTOMER_SERVICE = TemplateSpec(
    id="customer_service",
    name="Chăm sóc khách hàng",
    description=(
        "Khách hàng gửi câu hỏi hoặc khiếu nại, agent tra cứu tài liệu hỗ trợ liên quan, soạn một phản hồi, xin "
        "người quản lý duyệt rồi mới gửi thật cho khách qua hộp thư hoặc hệ thống đã kết nối, và báo lại kết quả. "
        "Hợp với đội chăm sóc khách hàng cần trả lời khách nhưng không được tự ý gửi khi chưa ai duyệt."
    ),
    start_node_id="search",
    nodes=[
        _node("search", NodeType.KNOWLEDGE_SEARCH, {"source_ids": []}, column=0),
        _node("draft", NodeType.AGENT_LLM, {"prompt": "", "model": ""}, column=1),
        _node(
            "approval",
            NodeType.HUMAN_APPROVAL,
            {"approver_role": _DEFAULT_APPROVER_ROLE, "message_template": ""},
            column=2,
        ),
        _node("send", NodeType.MCP, {"tool_id": "", "action": ""}, column=3),
        _node("report", NodeType.AGENT_LLM, {"prompt": "", "model": ""}, column=4, row=1),
        _node("finish", NodeType.END, {}, column=5, row=1),
    ],
    edges=[
        _edge("search", "found", "draft"),
        _edge("draft", "done", "approval"),
        # Both ways an approval ends — sent, or turned down — rejoin at one node,
        # the same reasoning `action_with_approval` uses: the customer is owed an
        # account of what happened either way.
        _edge("approval", "approved", "send"),
        _edge("approval", "rejected", "report"),
        _edge("send", "done", "report"),
        _edge("report", "done", "finish"),
    ],
    # A connected account is what `send` actually gets pointed at — no account
    # connected, nothing this shape can act through.
    required_data_kinds=[DataSourceKind.CONNECTED_ACCOUNT],
)

_DOCUMENT_COMPARISON = TemplateSpec(
    id="document_comparison",
    name="So sánh tài liệu",
    description=(
        "Người dùng cần so sánh nhiều tài liệu hoặc nhiều phiên bản đã gắn — ví dụ hai bản hợp đồng, hai bản "
        "chính sách — agent tìm trong các tài liệu đó rồi chỉ ra điểm giống và khác nhau. Không thực hiện hành "
        "động nào ra bên ngoài."
    ),
    start_node_id="search",
    nodes=[
        _node("search", NodeType.KNOWLEDGE_SEARCH, {"source_ids": []}, column=0),
        _node("compare", NodeType.AGENT_LLM, {"prompt": "", "model": ""}, column=1),
        _node("finish", NodeType.END, {}, column=2),
    ],
    edges=[
        _edge("search", "found", "compare"),
        _edge("compare", "done", "finish"),
    ],
    # Same reasoning as `document_qa` — only `uploaded_file` carries a Knowledge
    # Base id the `search` node can be pointed at.
    required_data_kinds=[DataSourceKind.UPLOADED_FILE],
)

_INTERNAL_KNOWLEDGE_ASSISTANT = TemplateSpec(
    id="internal_knowledge_assistant",
    name="Trợ lý tri thức nội bộ",
    description=(
        "Nhân viên nội bộ hỏi về quy trình, chính sách, tài liệu nội bộ của công ty, agent tìm trong kho tài "
        "liệu nội bộ đã gắn rồi trả lời dựa trên đúng những gì tìm được. Hợp với trợ lý tra cứu nội bộ cho nhân "
        "viên — không thực hiện hành động nào ra bên ngoài."
    ),
    start_node_id="search",
    nodes=[
        _node("search", NodeType.KNOWLEDGE_SEARCH, {"source_ids": []}, column=0),
        _node("answer", NodeType.AGENT_LLM, {"prompt": "", "model": ""}, column=1),
        _node("finish", NodeType.END, {}, column=2),
    ],
    edges=[
        _edge("search", "found", "answer"),
        _edge("answer", "done", "finish"),
    ],
    # ADR-0025 §9 — deliberately the same `KnowledgeSearch → AgentLlm → End`
    # shape `document_qa` uses. One tier, not two (§2): the two are the same
    # graph put to two different use cases, and the duplication is a few lines
    # here rather than a two-tier registry `template_id` would leave ambiguous.
    # Only `uploaded_file` carries a Knowledge Base id (ADR-0043 §1).
    required_data_kinds=[DataSourceKind.UPLOADED_FILE],
)

_DATA_ENRICHMENT = TemplateSpec(
    id="data_enrichment",
    name="Làm giàu dữ liệu",
    description=(
        "Agent nhận một danh sách bản ghi — khách hàng, lead, danh bạ — tìm thêm thông tin liên quan trên web "
        "rồi cập nhật/bổ sung thông tin đó vào hệ thống đã kết nối. Hợp với làm giàu dữ liệu khách hàng hoặc "
        "lead từ nguồn công khai."
    ),
    start_node_id="lookup",
    nodes=[
        _node("lookup", NodeType.WEB_SEARCH, {}, column=0),
        _node("enrich", NodeType.AGENT_LLM, {"prompt": "", "model": ""}, column=1),
        _node("update", NodeType.MCP, {"tool_id": "", "action": ""}, column=2),
        _node("finish", NodeType.END, {}, column=3),
    ],
    edges=[
        _edge("lookup", "done", "enrich"),
        _edge("enrich", "done", "update"),
        _edge("update", "done", "finish"),
    ],
    # `update` is a write, but not the kind `src.workflow.effects` calls
    # unrecoverable — a record's fields can be corrected again — so this shape
    # ships with no approval gate. A spec whose enrichment reads as high-risk
    # anyway (e.g. it also deletes records) is caught by the same rule
    # ADR-0025 §3 checks at selection time, not by this template's structure.
    required_data_kinds=[DataSourceKind.CONNECTED_ACCOUNT],
)


_DATABASE_QA = TemplateSpec(
    id="database_qa",
    name="Hỏi đáp trên dữ liệu nghiệp vụ",
    description=(
        "Người dùng hỏi về số liệu nghiệp vụ — công nợ, đơn hàng, tồn kho — agent truy vấn Hệ CSDL của công ty "
        "theo đúng phạm vi người đang hỏi được xem, rồi trả lời kèm trích dẫn nguồn. Không thực hiện hành động "
        "nào ra bên ngoài."
    ),
    start_node_id="query",
    nodes=[
        _node("query", NodeType.DATA_QUERY, {"source_id": "", "views": []}, column=0),
        _node("answer", NodeType.AGENT_LLM, {"prompt": "", "model": ""}, column=1),
        _node("finish", NodeType.END, {}, column=2),
    ],
    edges=[
        _edge("query", "done", "answer"),
        _edge("answer", "done", "finish"),
    ],
    # A Hệ CSDL is what `query` is bound to (DS-11) — none registered, nothing to read.
    required_data_kinds=[DataSourceKind.DATABASE],
)

_DATABASE_OUTREACH = TemplateSpec(
    id="database_outreach",
    name="Đọc dữ liệu nghiệp vụ rồi gửi thư có duyệt",
    description=(
        "Agent truy vấn Hệ CSDL của công ty theo đúng phạm vi người đang hỏi — ví dụ khách quá hạn công nợ — "
        "soạn một thư dựa trên đúng các dòng đọc được, xin người duyệt xem nguyên văn rồi mới gửi qua tài khoản "
        "đã kết nối, và báo lại kết quả. Hợp với nhắc nợ, nhắc giao hàng, thông báo cho khách."
    ),
    start_node_id="collect",
    nodes=[
        _node("collect", NodeType.COLLECT, {"action_node": "send", "agent_writes": ["subject", "body"]}, column=0),
        _node("query", NodeType.DATA_QUERY, {"source_id": "", "views": []}, column=1),
        _node("draft", NodeType.AGENT_LLM, {"prompt": "", "model": ""}, column=2),
        _node(
            "approval",
            NodeType.HUMAN_APPROVAL,
            {"approver_role": _DEFAULT_APPROVER_ROLE, "message_template": ""},
            column=3,
        ),
        _node("send", NodeType.MCP, {"tool_id": "", "action": ""}, column=4),
        _node("report", NodeType.AGENT_LLM, {"prompt": "", "model": ""}, column=5, row=1),
        _node("finish", NodeType.END, {}, column=6, row=1),
    ],
    edges=[
        _edge("collect", "complete", "query"),
        # The question the collector asked is the turn's answer (ADR-0052).
        _edge("collect", "incomplete", "finish"),
        _edge("query", "done", "draft"),
        _edge("draft", "done", "approval"),
        _edge("approval", "approved", "send"),
        _edge("approval", "rejected", "report"),
        _edge("send", "done", "report"),
        _edge("report", "done", "finish"),
    ],
    # DS-11's demo shape: `collect → data_query → agent_llm → human_approval →
    # <action>`. Both a Hệ CSDL and an account are needed, but the selector's
    # rule is "any one of", so the one it cannot do without is named.
    required_data_kinds=[DataSourceKind.DATABASE],
)


#: The built-in seed catalog. Adding an entry is a code change reviewed like any
#: other — which is exactly the property ADR-0025 §7 protects by refusing to let
#: a freehand or LLM-drawn graph enter here.
TEMPLATE_REGISTRY: dict[str, TemplateSpec] = {
    _DOCUMENT_QA.id: _DOCUMENT_QA,
    _ACTION_WITH_APPROVAL.id: _ACTION_WITH_APPROVAL,
    _DIRECT_ASSISTANT.id: _DIRECT_ASSISTANT,
    _PLANNING_HELPER.id: _PLANNING_HELPER,
    _CUSTOMER_SERVICE.id: _CUSTOMER_SERVICE,
    _DOCUMENT_COMPARISON.id: _DOCUMENT_COMPARISON,
    _INTERNAL_KNOWLEDGE_ASSISTANT.id: _INTERNAL_KNOWLEDGE_ASSISTANT,
    _DATA_ENRICHMENT.id: _DATA_ENRICHMENT,
    _DATABASE_QA.id: _DATABASE_QA,
    _DATABASE_OUTREACH.id: _DATABASE_OUTREACH,
}


def approved_templates(db: "Session | None" = None) -> Sequence[TemplateSpec]:
    """Every template the Generator is allowed to propose, in registry order.

    The one place `built_in` code entries and approved creator submissions are
    joined (ADR-0025 §6). `pending`, `rejected` and `disabled` entries never
    appear — and because a produced workflow copies its template's graph rather
    than referencing it, disabling one changes nothing about workflows already
    generated from it.

    `db` is optional and defaults to `None` — every caller this module shipped
    with before #53 keeps working unchanged, seeing only the built-in seed. A
    caller that *can* reach a session (the Generator, by the time it selects a
    template, always can) passes it to see `creator_submitted` entries too. The
    import of `src.workflow.submissions` is deferred to inside this branch
    because that module reads `src.db.models`, which itself reads
    `TemplateStatus`/`TemplateSource` from here — importing it at module load
    time would be a cycle neither side can resolve first.
    """
    built_in = [template for template in TEMPLATE_REGISTRY.values() if template.status is TemplateStatus.APPROVED]
    if db is None:
        return built_in
    from src.workflow.submissions import approved_submission_specs

    return [*built_in, *approved_submission_specs(db)]


def template_of(template_id: str, db: "Session | None" = None) -> TemplateSpec | None:
    """The template called `template_id`, or `None` if no approved entry has that id.

    `None` rather than a raise, because the caller asking is usually holding an
    id a language model just proposed. An invented id is a proposal to reject
    and ask again about (ADR-0025 §3), not an exception to handle.

    Checks the built-in registry first and only reaches for `db` — same deferred
    import, same reason — when `template_id` is not one of the seven seed ids,
    which is the common case and the one that must stay a plain dict lookup.
    """
    template = TEMPLATE_REGISTRY.get(template_id)
    if template is not None:
        return template if template.status is TemplateStatus.APPROVED else None
    if db is None:
        return None
    from src.workflow.submissions import approved_submission_spec_of

    return approved_submission_spec_of(db, template_id)
