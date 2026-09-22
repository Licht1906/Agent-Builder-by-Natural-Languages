"""`WorkflowJSON` — the document a V2 agent actually runs as, per ADR-0022.

Three parties hold this one shape between them: the Workflow Generator (#43)
writes it from a confirmed spec, the Workflow Canvas (#44) edits it, and the
Workflow Runtime executes it. It replaces V1's `AgentSpec.steps` — a linear list
where `next: null` meant "the next one down" — with a real graph that branches,
loops and stops to wait for a person.

Flat on purpose: `nodes`, `edges`, `start_node_id` and `metadata` are peers, not
a document of documents, which is both V1's precedent and exactly what a
drag-and-drop canvas serializes.

**This module validates the envelope and nothing about the graph.** A node's
`config` is stored as it arrived, an edge may point at a node that no longer
exists, a branch may lead nowhere. That is not an oversight — ADR-0022 §5–6
promises the creator that a mid-edit draft always saves, and the structural
rules (cycles, one edge per output port, every path reaching `End`) are enforced
once, at the `workflow_pending_confirmation → workflow_confirmed` gate, by
`src.workflow.validation`. Putting them here would turn every keystroke on the
canvas into a document the backend refuses to store.

The one thing it does refuse is a budget above `SYSTEM_LIMITS`. That ceiling is
not a statement about this graph being finished — it is the outer bound V1 never
let any spec exceed (ADR-0001 §5), and a draft is not a reason to let a run cost
more than the platform allows.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.models.agent_spec import SYSTEM_LIMITS
from src.models.node_registry import NodeType


def _non_blank(value: str) -> str:
    """`value` stripped, refusing anything that holds nothing.

    The same rule `src.models.solution_spec` applies to the spec's text, for the
    same reason: an id of spaces is an id nothing can be looked up by, and it
    would satisfy every "this field is filled" check made about the document.
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("không được để trống")
    return cleaned


class Position(BaseModel):
    """Where a node sits on the canvas.

    Stored on the document rather than derived by a layout pass, because the
    creator moved it there. Auto-layout would quietly undo the arrangement they
    made every time the workflow was re-opened.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    x: float = 0.0
    y: float = 0.0


class NodeInstance(BaseModel):
    """One node on the graph: which type it is, how it is configured, where it sits.

    `config` is a plain mapping, not the type's `NodeConfig` model, and the
    difference is the whole draft/confirm split. Validating it here would refuse
    to store a node the creator has half-filled in; `src.workflow.validation`
    checks it against `NODE_REGISTRY` at confirm time, when being finished is
    finally the promise being made.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., description="Id của node trong graph, là thứ mọi cạnh trỏ tới")
    type: NodeType = Field(..., description="Một trong tám loại node của NODE_REGISTRY")
    config: dict[str, Any] = Field(default_factory=dict, description="Cấu hình theo config_schema của loại node đó")
    position: Position = Field(default_factory=Position, description="Vị trí trên canvas")

    _strip_id = field_validator("id")(_non_blank)


class Edge(BaseModel):
    """One connection: out of a named port on one node, into another node.

    There is no `target_port`, and its absence is a decision (ADR-0022 §2): an
    input port has no name and accepts any number of edges, so a target port
    field would have nothing to disambiguate. Edges live in their own list
    rather than inside the node they leave, which makes "at most one edge per
    output port" a count over `(source_node_id, source_port)` instead of
    something the validator reassembles from scattered node fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(..., description="Id của cạnh, để canvas xoá đúng một cạnh")
    source_node_id: str = Field(..., description="Node mà cạnh đi ra từ đó")
    source_port: str = Field(..., description="Một trong các output port có tên của loại node nguồn")
    target_node_id: str = Field(..., description="Node mà cạnh đi tới")

    _strip_ids = field_validator("id", "source_node_id", "source_port", "target_node_id")(_non_blank)


class WorkflowBudget(BaseModel):
    """What one run of this workflow is allowed to spend before it is stopped.

    ADR-0020 kept the Agent Solution Specification free of anything technical, so
    the ceiling V1 carried on `AgentSpec.budget` has no home until a technical
    document exists — this is that document (ADR-0022 §3). `BudgetLedger`
    (ADR-0008) reads it at run start exactly as it read V1's.

    Bounded by `SYSTEM_LIMITS` here rather than at the run: a creator raising the
    ceiling on the canvas should be told at the moment they do it, not have the
    number accepted and silently clamped by something they never see.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_total_tokens: int = Field(..., gt=0, description="Trần token cho cả một lần chạy")
    max_wall_seconds: int = Field(..., gt=0, description="Trần thời gian chạy, tính bằng giây")

    @model_validator(mode="after")
    def _stays_under_the_system_ceiling(self) -> "WorkflowBudget":
        if self.max_total_tokens > SYSTEM_LIMITS.max_total_tokens:
            raise ValueError(
                f"vượt trần token của hệ thống: {self.max_total_tokens} > {SYSTEM_LIMITS.max_total_tokens}"
            )
        if self.max_wall_seconds > SYSTEM_LIMITS.max_wall_seconds:
            raise ValueError(
                f"vượt trần thời gian của hệ thống: {self.max_wall_seconds} > {SYSTEM_LIMITS.max_wall_seconds}"
            )
        return self


#: What the Workflow Generator writes when it drafts a workflow — not the
#: creator, and never the interview (ADR-0022 §3). The creator may raise it on
#: the canvas, up to `SYSTEM_LIMITS`.
#:
#: These were half the system ceiling (100k tokens / 150s) on the argument that
#: no demo workflow would hit them by accident. True, but far truer than
#: needed: measured against the run ledger, the most expensive real run this
#: platform has ever recorded spent **2,790 tokens in 15.9 seconds** (P50 was
#: 1,135 / 3.7s across 209 production runs). A ceiling 36× above the worst
#: case ever observed does not brake a runaway, it only records one — and the
#: cost of that gap is real money, because a loop that should have been cut at
#: a few thousand tokens is free to spend a hundred thousand.
#:
#: 20,000 is ~7× the worst run observed: room for a workflow with a real RAG
#: context or a `While` body, still five times tighter than before. 60 seconds
#: is not a guess either — it is `PM_THRESHOLDS.latency_p90_max_seconds`, the
#: number the PM board already calls "too slow". A run the board would flag is
#: a run the runtime should have stopped.
DEFAULT_WORKFLOW_BUDGET = WorkflowBudget(max_total_tokens=20_000, max_wall_seconds=60)


class WorkflowMetadata(BaseModel):
    """Everything about the workflow that is not a node or an edge.

    There is no `allowed_tools` (ADR-0022 §3): which tools a run may call is
    read straight off the `MCP`/`KnowledgeSearch`/`WebSearch` nodes present in
    the graph, and a separate list would be a second place that answer lives,
    drifting from the graph it claims to describe. `disclaimer` is not that
    kind of field — it names nothing the graph already says, so it earns its
    place here rather than being read from a second document.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    budget: WorkflowBudget = Field(
        default=DEFAULT_WORKFLOW_BUDGET, description="Trần chi phí một lần chạy, trong giới hạn hệ thống"
    )
    template_id: str | None = Field(
        default=None,
        description="Template mà Workflow Generator đã chọn; null với graph người tạo tự vẽ",
    )
    disclaimer: str = Field(
        default="",
        description=(
            "Dòng miễn trừ trách nhiệm hiển thị dưới mỗi câu trả lời của agent này (RAG-14 / #100) — "
            "cấu hình theo từng agent, nền tảng không hardcode một câu chung cho mọi agent"
        ),
    )
    fallback_reason: str | None = Field(
        default=None,
        description=(
            "Vì sao đây là mẫu gần nhất chứ không phải graph soạn riêng: lý do composer bị từ chối. "
            "Null khi graph được soạn riêng hay người tạo tự chọn mẫu — ghi lên tài liệu để canvas nói "
            "ra được điều mà trước đây chỉ một dòng log ở server biết"
        ),
    )


class WorkflowJSON(BaseModel):
    """A whole workflow: where it starts, what is in it, and what it may spend.

    Frozen, like every other stored document here. Editing on the canvas writes
    a new document over the draft; editing after `workflow_confirmed` produces a
    new `AgentVersion` (ADR-0022 §5). Neither one is a mutation of this object.

    `nodes` refuses to be empty while `edges` does not, and that pairing is the
    mid-edit case: a node dropped on the canvas and not yet wired to anything is
    exactly the draft ADR-0022 §5 promises will save. A workflow with no nodes at
    all has no node its `start_node_id` could name, and ADR-0021 §4 does not let
    that field be absent or guessed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_node_id: str = Field(..., description="Node chạy đầu tiên — luôn tường minh, không suy luận")
    nodes: list[NodeInstance] = Field(..., min_length=1, description="Các node của graph")
    edges: list[Edge] = Field(default_factory=list, description="Các cạnh nối giữa các node")
    metadata: WorkflowMetadata = Field(default_factory=WorkflowMetadata, description="Ngân sách và template gốc")

    _strip_start = field_validator("start_node_id")(_non_blank)

    def node(self, node_id: str) -> NodeInstance | None:
        """The node called `node_id`, or `None` if the graph holds no such node.

        `None` rather than a raise: an edge pointing at a deleted node is a
        mid-edit draft, and the validator reports it as a problem — it is not an
        error for a caller to *ask*.
        """
        return next((node for node in self.nodes if node.id == node_id), None)

    def nodes_of_type(self, node_type: NodeType) -> list[NodeInstance]:
        """Every node of `node_type` in the graph, in document order.

        How "which tools does this workflow actually call" gets answered without
        an `allowed_tools` field to keep in step.
        """
        return [node for node in self.nodes if node.type is node_type]
