"""The nine node types a V2 workflow is built from, and nothing else — ADR-0021, ADR-0052.

A closed, code-defined registry, exactly the shape `TOOL_REGISTRY` and
`MODEL_CATALOG` gave V1: one entry per type, holding what that type configures
and where it can send control next. A node inside a `WorkflowJSON` only ever
names its type — it never re-declares the type's config shape or its ports — so
the canvas drawing a property form, the validator counting edges out of a port,
and the Workflow Generator filling a placeholder are all reading this one table.
Two copies of that knowledge would drift into a node the canvas offers and the
validator refuses, with nothing on screen saying why.

Adding a node type is a change here plus a runtime handler for it, the same
two-sided change V1 needed for a new tool — `collect` (ADR-0052) was the first
to go through it. Neither a spec nor a workflow can invent one.

The config shapes are Pydantic models rather than hand-written JSON Schema
documents, and `config_schema` derives the schema from the model. A hand-written
schema is a second description of the same shape, and the way those two drift is
that a field gains a type in one and not the other.

Every config field carries a default, which looks odd for a document that has to
be complete before it runs. It is deliberate: ADR-0022 §5–6 promises a mid-edit
draft always saves, so a node the Generator (or the creator) has not finished
configuring must be a *valid instance with blanks in it*, not a parse error.
Which blanks are still blank is `unfilled_fields`, and confirming is where they
stop being acceptable.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NodeType(StrEnum):
    """The eight ADR-0021 fixed for the MVP, plus `collect` (ADR-0052).

    Written snake_case on the wire like every other enum in the platform, rather
    than the `AgentLlm` spelling the ADRs use in prose — a stored document and a
    canvas payload are read by the same validators that read `spec_confirmed`
    and `uploaded_file`, and one casing convention is one less thing to get
    wrong at a boundary.
    """

    AGENT_LLM = "agent_llm"
    END = "end"
    KNOWLEDGE_SEARCH = "knowledge_search"
    MCP = "mcp"
    WEB_SEARCH = "web_search"
    IF_ELSE = "if_else"
    WHILE = "while"
    HUMAN_APPROVAL = "human_approval"
    COLLECT = "collect"


class NodeConfig(BaseModel):
    """What every node type's configuration has in common: nothing but its rules.

    `extra="forbid"` is the rule that matters. A field this platform never reads
    is worse than a missing one — the creator believes they configured
    something, and the run behaves as though they never touched it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


#: Marks a config field as holding **business prose** — a sentence the creator
#: (or the Generator's model) writes, rather than a reference, a name or a number.
#:
#: Two very different readers ask the same question of it, which is why it is a
#: property of the field rather than a list kept somewhere. `src.workflow.fill`
#: uses it to decide what an LLM is allowed to write, because no rule can compose
#: a meaningful prompt out of a spec's wording; the Workflow Canvas (#44) uses it
#: to decide which inputs are a text area rather than a one-line box, because a
#: prompt typed into a single-line field is a prompt nobody can read back.
#:
#: Two hand-kept lists would answer it differently the first time a ninth field
#: is added, and the way that shows up is a node whose prompt the Generator fills
#: and the canvas will not let anybody edit properly.
#:
#: Annotated `dict[str, Any]` rather than inferred: `json_schema_extra` is typed
#: to take Pydantic's `JsonDict`, and a `dict[str, bool]` is not one of those —
#: `dict` is invariant in its value type.
PROSE: dict[str, Any] = {"multiline": True}


def is_prose_field(node_type: NodeType, field: str) -> bool:
    """Whether `field` of `node_type` holds business prose — see `PROSE`."""
    declared = config_model_of(node_type).model_fields.get(field)
    return bool(declared is not None and declared.json_schema_extra == PROSE)


def prose_fields(node_type: NodeType) -> tuple[str, ...]:
    """`node_type`'s prose fields, in declaration order."""
    return tuple(name for name in config_model_of(node_type).model_fields if is_prose_field(node_type, name))


#: Marks a config field as **addressing a resource** — a file, connection or
#: action that exists because a particular creator attached or registered it,
#: rather than a name the platform or the graph shape invented.
#:
#: The mirror image of `PROSE`, and a property of the field for the same reason:
#: `src.workflow.fill` reads a spec's own data to fill these by rule rather than
#: ever asking a model to guess one (ADR-0025 §5), and `src.workflow.templates`
#: reads the same marker to know which fields "save as template" must clear to a
#: placeholder rather than copy verbatim — reusing the submitting creator's own
#: file key or connection id in somebody else's template would point that
#: template at resources it has no business touching (ADR-0025 §8).
RESOURCE_REF: dict[str, Any] = {"resource_ref": True}


def is_resource_field(node_type: NodeType, field: str) -> bool:
    """Whether `field` of `node_type` addresses a specific creator's resource — see `RESOURCE_REF`."""
    declared = config_model_of(node_type).model_fields.get(field)
    return bool(declared is not None and declared.json_schema_extra == RESOURCE_REF)


def resource_fields(node_type: NodeType) -> tuple[str, ...]:
    """`node_type`'s resource-reference fields, in declaration order."""
    return tuple(name for name in config_model_of(node_type).model_fields if is_resource_field(node_type, name))


#: Marks a config field as a **refinement**: leaving it blank is a complete
#: answer, not an unanswered question.
#:
#: Every other field in this registry is something a node cannot run without, so
#: `unfilled_fields` treats blank as *not yet configured* and confirmation
#: refuses it. That rule is right for a prompt and wrong for a field whose whole
#: purpose is to narrow a behaviour that already has a sensible unnarrowed form.
#: Without this marker the only way to add one is to give it a non-blank default
#: that means "no value" — a sentinel every reader then has to know about — or to
#: make a working node unconfirmable the day the field is added, which is what
#: adding `query_hint` to `WebSearchConfig` would otherwise have done to every
#: `WebSearch` node already drawn (ADR-0042).
#:
#: Deliberately not composed with `PROSE` or `RESOURCE_REF`: those two are
#: matched by equality on `json_schema_extra`, so a field carrying two markers
#: would silently match neither. A refinement the Generator should be writing
#: wants `PROSE` and a different mechanism for optionality; nothing needs both
#: yet, and inventing the combination before there is a second case would be
#: guessing at which of the two rules the third field wants.
OPTIONAL: dict[str, Any] = {"optional": True}


def is_optional_field(node_type: NodeType, field: str) -> bool:
    """Whether `field` of `node_type` may be left blank on a finished graph — see `OPTIONAL`."""
    declared = config_model_of(node_type).model_fields.get(field)
    return bool(declared is not None and declared.json_schema_extra == OPTIONAL)


def optional_fields(node_type: NodeType) -> tuple[str, ...]:
    """`node_type`'s refinement fields, in declaration order."""
    return tuple(name for name in config_model_of(node_type).model_fields if is_optional_field(node_type, name))


class AgentLlmConfig(NodeConfig):
    """A model, and what to ask it.

    No `tools` field, and that absence is ADR-0021 §2's whole argument: using a
    tool means drawing a tool node and wiring it in, so the graph stays an
    accurate account of what a run touched instead of a list of what it was
    allowed to touch.
    """

    prompt: str = Field(default="", description="Câu lệnh cho model ở bước này", json_schema_extra=PROSE)
    model: str = Field(default="", description="Model chạy bước này, phải nằm trong allowlist của admin")


class EndConfig(NodeConfig):
    """Nothing to configure — reaching it *is* the behaviour."""


class KnowledgeSearchConfig(NodeConfig):
    """Which Knowledge Base shelves this node searches.

    `source_ids` holds Knowledge Base ids — never a Document, never a file name
    (ADR-0043 §1). A node cannot be pointed at a single file the creator
    uploaded; it is pointed at a shelf that can gain and lose Documents without
    that being a change to this config. Attached by a person, on the spec
    screen or the canvas, never a name a model read off a description
    (ADR-0025 §5).
    """

    source_ids: list[str] = Field(
        default_factory=list,
        description="Id của các Knowledge Base được tìm trong",
        json_schema_extra=RESOURCE_REF,
    )


class McpApproval(StrEnum):
    """Whether this node insists on a person, over and above what the platform worked out.

    OpenAI's Agent Builder gives its MCP node a `require_approval` of
    `"always"` | `"never"` | a per-tool mapping. Two of those three are ported
    here and the third deliberately is not, because the two platforms disagree
    about who owns the answer. There, approval is a setting on the tool call;
    here it is *derived from the action's effect* (`src.workflow.tools.risk_of`
    over `src.workflow.mcp.reads_only`) and then enforced on the graph
    (ADR-0023 §5), precisely so that the party being gated cannot be the party
    that lifts the gate.

    So this field may only **tighten**:

    - `auto` — the platform's own answer. A `send_email` is guarded whatever
      this says; an inbox read is not. The default, and what every `MCP` node
      drawn before this field existed goes on meaning.
    - `always` — the creator wants a person in front of this node even though
      the action is a read the platform would have let through. Their own
      business rule ("nobody touches the supplier list unattended"), which the
      platform has no way to infer and no business overruling.

    There is no `never`, and its absence is the point rather than an omission:
    it would let a creator switch off ADR-0023 §5 for exactly the node the rule
    was written about. Accepting the value and silently treating it as `auto`
    would be worse still — the form would say the gate is off while the
    validator kept it on.
    """

    AUTO = "auto"
    ALWAYS = "always"


class McpActionMode(StrEnum):
    """Whether this node calls one named action, or hands the model a set to choose from.

    The port of Agent Builder's `allowed_tools`, which is the field ADR-0021 §2
    originally refused. The refusal had a real argument behind it — a graph
    naming one action is an accurate account of what a run touched, where a
    graph naming a *permission* is only an account of what it was allowed to
    touch — and `single` keeps that account exactly as it was.

    What changed is the recognition that the argument does not hold for every
    step. A node whose job is "look this customer up, whichever of the three
    lookups fits" is a node whose action genuinely is not known until the run,
    and forcing it into one name produces either three nodes and a branch, or a
    lie. `full` is for that node, and it pays for the looseness with evidence:
    the run's transcript still records the action actually called, so the
    *account* stays accurate even when the *drawing* cannot be.

    The safety floor does not move between the two. A `full` node's risk is the
    highest risk among the actions it may call, so a set containing `send_email`
    is guarded exactly as a `single` node naming `send_email` is.
    """

    SINGLE = "single"
    FULL = "full"


class McpConfig(NodeConfig):
    """One connected MCP tool — one named action, or a set the model picks from.

    ADR-0023 §2's two-level reference, now with Agent Builder's `allowed_tools`
    beside it (see `McpActionMode`). Which of a connection's actions are
    *offered at all* remains a property of the connection
    (`ToolRegistration.allowed_actions`), decided once where an admin reviews
    it; `allowed_actions` here can only narrow that set further, never widen it.
    """

    tool_id: str = Field(
        default="", description="Id của ToolDefinition (một kết nối/MCP server)", json_schema_extra=RESOURCE_REF
    )
    action: str = Field(
        default="", description="Tên action trong `actions[]` của tool đó", json_schema_extra=RESOURCE_REF
    )
    action_mode: McpActionMode = Field(
        default=McpActionMode.SINGLE,
        description=(
            "'single': node gọi đúng action đã chọn. "
            "'full': model tự chọn trong số các action được phép của kết nối"
        ),
        # `OPTIONAL` for `require_approval`'s reason: every MCP node stored
        # before this field existed simply has no key for it, and absent means
        # `single` — which is exactly what those nodes already did.
        json_schema_extra=OPTIONAL,
    )
    allowed_actions: list[str] = Field(
        default_factory=list,
        description=(
            "Chỉ dùng khi action_mode='full': giới hạn các action model được chọn. "
            "Rỗng = mọi action kết nối này cho phép"
        ),
        json_schema_extra=OPTIONAL,
    )
    require_approval: McpApproval = Field(
        default=McpApproval.AUTO,
        description="'auto' để nền tảng tự quyết theo mức rủi ro; 'always' để luôn bắt buộc có người duyệt",
        # `OPTIONAL` for the reason ADR-0042 gives about `WebSearchConfig.query_hint`,
        # and it is not decoration: `unfilled_fields` reads a *stored* config dict,
        # where a field added today is simply absent — so without this marker every
        # `MCP` node already drawn on the platform would be reported unconfigured and
        # refuse to confirm, on the day this field shipped. Absent is not unanswered
        # here; it is `auto`, which is the whole of what those nodes already meant.
        json_schema_extra=OPTIONAL,
    )


class WebSearchConfig(NodeConfig):
    """The subject is still what the graph hands it — these only narrow the search.

    Both fields are `OPTIONAL` because a `WebSearch` node with neither is exactly
    the node this type was before ADR-0042, and every one already drawn has to go
    on confirming.

    `query_hint` is appended to the subject rather than substituted into it:
    there is no placeholder syntax in a node config to substitute with, and
    inventing one here would be inventing document structure the canvas cannot
    edit — the same reason `HumanApprovalConfig.message_template` is used as
    written.

    What the *subject* is stopped being "the user's whole sentence" in ADR-0068:
    an `AgentLlm` node drawn immediately in front of this one is an extractor and
    what it wrote is the query. That is a fact about the graph's shape, so it is
    still not a field here — there is nothing for a creator to fill in.
    """

    query_hint: str = Field(
        default="",
        description="Từ khoá thêm vào câu hỏi để thu hẹp tìm kiếm, ví dụ 'dự báo hôm nay'",
        json_schema_extra=OPTIONAL,
    )
    country: str = Field(
        default="",
        description="Ưu tiên kết quả từ một quốc gia, ví dụ 'vietnam'",
        json_schema_extra=OPTIONAL,
    )


class IfElseConfig(NodeConfig):
    """The question the branch turns on, in the creator's own business language."""

    condition: str = Field(
        default="", description="Điều kiện rẽ nhánh, viết bằng ngôn ngữ nghiệp vụ", json_schema_extra=PROSE
    )


class WhileConfig(NodeConfig):
    """A condition and the ceiling that keeps it from running forever.

    `max_iterations` is not optional and has a non-zero default, because a
    `While` node's `loop` port is the only place in a V2 graph a cycle is legal
    (ADR-0021 §5). A cycle with no cap is the failure that rule exists to
    prevent, so the cap ships with the node rather than waiting to be filled in.
    """

    condition: str = Field(default="", description="Còn đúng thì còn lặp", json_schema_extra=PROSE)
    max_iterations: int = Field(default=5, ge=1, description="Trần số vòng lặp, bắt buộc và luôn hữu hạn")


class HumanApprovalConfig(NodeConfig):
    """Who is asked, and what they are shown when the run stops in front of them."""

    approver_role: str = Field(default="", description="Vai trò của người được hỏi, ví dụ 'manager'")
    message_template: str = Field(
        default="", description="Nội dung yêu cầu duyệt gửi tới người đó", json_schema_extra=PROSE
    )


#: The class of a config model, spelled once here because `NodeTypeSpec` has a
#: field called `type` — inside that class body, a bare `type[NodeConfig]`
#: annotation reads as the field rather than the builtin.
NodeConfigClass = type[NodeConfig]


class CollectConfig(NodeConfig):
    """Which `MCP` node this collector gathers the user's details for — and nothing to write.

    The ninth type (ADR-0052), and the only one whose behaviour is **derived**
    rather than configured: what it asks for is the required parameters of the
    action bound on `action_node`, read off that action's `input_schema` at run
    time. There is no prompt to write and no condition to phrase, which is the
    point. Before it existed, "has the user given everything the action
    needs?" was an `AgentLlm` collector plus an `IfElse` gate, both written in
    prose by the meta agent — and that prose is where every field failure of
    the Google agents lived: a gate demanding *email người chủ trì* (no such
    parameter), a collector asking *bao nhiêu thư?* for an optional count, a
    gate that said "chưa đủ" and took the `true` port anyway. A parameter list
    read from the schema cannot name a parameter that does not exist, cannot
    ask for an optional one, and cannot disagree with itself.

    `agent_writes` names the parameters a *later* node composes rather than
    the user supplies — `body` of an invitation the graph drafts from the
    event details — so the collector does not stop the run to ask the user for
    prose the agent was built to write. Optional (`OPTIONAL`): blank means every
    required parameter comes from the user.

    Two ports. `complete` carries on to whatever the creator drew next (an
    approval, the action, a composing step); `incomplete` is where the question
    the collector just asked becomes the turn's answer, so it leads to `end` —
    the run stops and waits for the user's next message, which is the only way
    a run ever waits for a person (ADR-0021 §5, ADR-0026).
    """

    action_node: str = Field(default="", description="Id của node mcp mà node này thu thập tham số cho")
    agent_writes: list[str] = Field(
        default_factory=list,
        description="Tham số do một node sau soạn ra (ví dụ body), không hỏi người dùng",
        json_schema_extra=OPTIONAL,
    )


@dataclass(frozen=True, slots=True)
class NodeTypeSpec:
    """One node type's whole contract: what it configures, and where it can go next.

    `outputs` is a tuple of *named* ports rather than a count, because the names
    are what an `Edge` points at (`"true"`, `"loop"`, `"approved"`) — a branch
    whose ports were positional would make every edge in a stored document
    depend on the order this file happens to list them in.
    """

    type: NodeType
    config_model: NodeConfigClass
    outputs: tuple[str, ...]

    @property
    def config_schema(self) -> dict[str, Any]:
        """This type's config as JSON Schema — what the canvas builds a form from.

        Derived, never authored. A hand-written schema beside the model is a
        second description of one shape, and it drifts silently.
        """
        return self.config_model.model_json_schema()


#: The types, in the order ADR-0021 §2 lists them with `collect` (ADR-0052) in
#: front of the `mcp` node it serves. Every consumer reads this table; nothing
#: else is allowed to describe a node type.
NODE_REGISTRY: dict[NodeType, NodeTypeSpec] = {
    NodeType.AGENT_LLM: NodeTypeSpec(NodeType.AGENT_LLM, AgentLlmConfig, ("done",)),
    NodeType.END: NodeTypeSpec(NodeType.END, EndConfig, ()),
    NodeType.KNOWLEDGE_SEARCH: NodeTypeSpec(NodeType.KNOWLEDGE_SEARCH, KnowledgeSearchConfig, ("found",)),
    NodeType.COLLECT: NodeTypeSpec(NodeType.COLLECT, CollectConfig, ("complete", "incomplete")),
    NodeType.MCP: NodeTypeSpec(NodeType.MCP, McpConfig, ("done",)),
    NodeType.WEB_SEARCH: NodeTypeSpec(NodeType.WEB_SEARCH, WebSearchConfig, ("done",)),
    NodeType.IF_ELSE: NodeTypeSpec(NodeType.IF_ELSE, IfElseConfig, ("true", "false")),
    NodeType.WHILE: NodeTypeSpec(NodeType.WHILE, WhileConfig, ("loop", "exit")),
    NodeType.HUMAN_APPROVAL: NodeTypeSpec(NodeType.HUMAN_APPROVAL, HumanApprovalConfig, ("approved", "rejected")),
}


def outputs_of(node_type: NodeType) -> tuple[str, ...]:
    """The named output ports `node_type` may send control out of."""
    return NODE_REGISTRY[node_type].outputs


def config_model_of(node_type: NodeType) -> type[NodeConfig]:
    """The Pydantic model `node_type`'s `config` has to satisfy."""
    return NODE_REGISTRY[node_type].config_model


def unfilled_fields(node_type: NodeType, config: dict[str, Any]) -> tuple[str, ...]:
    """The config fields of `config` that are still waiting for a value.

    Blankness is a property of text and of lists, and of nothing else: an empty
    string is a prompt nobody wrote and an empty `source_ids` is a search over
    nothing, but `0` is a number somebody chose. A field that is missing
    entirely reads the same as one left blank — both are the creator's answer
    still being absent, and telling them apart would only make the canvas say
    two different sentences about one empty box.

    Reported rather than refused, because ADR-0022 §5 promises a half-configured
    draft still saves. Confirming (#44) is where this list has to be empty.

    Fields marked `OPTIONAL` are skipped entirely: blank is their finished state,
    so reporting one would put a red ring on a node the creator has nothing left
    to do to, and — because every problem blocks confirmation — would strand a
    graph that is complete.
    """
    fields = config_model_of(node_type).model_fields
    waiting = tuple(
        name for name in fields if not is_optional_field(node_type, name) and _is_blank(config.get(name))
    )
    return tuple(name for name in waiting if name not in _answered_by_another_field(node_type, config))


def _answered_by_another_field(node_type: NodeType, config: dict[str, Any]) -> frozenset[str]:
    """Fields whose blankness is the *correct* state given what else is filled in.

    One case so far, and it is a real one rather than a convenience: an `MCP`
    node in `full` mode has no single `action` by definition — the model chooses
    at run time from the connection's allowed set — so demanding one would
    strand a finished node at the confirm gate. Kept as its own function rather
    than an `if` inside `unfilled_fields` because "blank is wrong" is the rule
    and every exception to it deserves to be listed somewhere a reader can find
    all of them at once.
    """
    if node_type is NodeType.MCP and str(config.get("action_mode", "")) == McpActionMode.FULL.value:
        return frozenset({"action"})
    return frozenset()


def _is_blank(value: Any) -> bool:
    """Whether `value` is an answer that was never actually given."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return not value
    return False
