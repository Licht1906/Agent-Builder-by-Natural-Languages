"""Edit a workflow graph by saying what to change — the canvas's second hand.

ADR-0022 §5 settled that the canvas is where a drafted graph gets fixed, and that
is still true: this is not a rival editor, it is the same document written by a
model instead of by a mouse. What it buys is the class of edit a canvas is bad
at — *"thêm bước duyệt tay trước khi gửi email"* is one sentence and four gestures
(place a node, rewire two edges, write a message) — and what it costs is one more
writer of `WorkflowJSON`, which is why the two invariants below are enforced in
code rather than asked for in the prompt.

**Resource fields are not the model's to write.** `tool_id`, `action`,
`source_ids` name a connection or a file that exists because a particular creator
registered or attached it (`RESOURCE_REF`, ADR-0025 §5). A model asked to keep
one will usually copy it and occasionally invent a plausible neighbour, and an
invented one points a graph at somebody else's resource. So they are copied
across from the node of the same id and type, or left blank on a node that did
not exist before — blank shows up in `problems` and gets a dropdown in the
Inspector, which is the creator answering rather than the model guessing. This is
the mirror of `src.workflow.fill._prose`, which lets a model write *only* prose.

**Positions are not the model's to write either.** A node that already exists
keeps the coordinates it had, because the whole point of editing on a canvas is
watching one thing change while everything else stays put; a graph that
re-arranges itself every turn is one nobody can read a diff off. New nodes are
placed by `_placed`, downstream of whatever feeds them.

What the model *does* decide is everything else: which nodes exist, what type
each is, how they are wired, and every prose field. Whether those changes stay
**local** — whether a node nobody mentioned comes back untouched — is asked for
in the prompt and checked in the tests, not enforced by a validator, exactly as
ADR-0016 §2 states for the V1 path.

Validation here is `WorkflowJSON.model_validate` and nothing more, which is the
envelope and not the graph (`src.models.workflow`). Asking `graph_problems` to
pass would make a chat edit stricter than a canvas edit — a creator can drag a
node into a half-wired state and save it, and being unable to *say* the same
thing would be a second set of rules for one document.
"""

import json
from collections.abc import Iterable
from typing import Any

from src.db.models import PlatformCallPurpose
from src.evals.model_json import json_object
from src.models.node_registry import NODE_REGISTRY, NodeType, config_model_of, is_resource_field, outputs_of
from src.models.workflow import NodeInstance, Position, WorkflowJSON
from src.services.llm import ChatModelFactory
from src.services.model_document import write_document

#: How far apart `_placed` puts a new node from the one that feeds it. Wide
#: enough that the two boxes and the edge between them are all readable at once
#: — `NODE_W` on the canvas is 210 — and not so wide that a three-node insert
#: leaves the graph off the right of the screen.
_STRIDE_X = 260

#: The drop `_placed` uses when a new node has nowhere to sit beside, and the
#: nudge it uses when the spot it picked is taken. One node height plus room for
#: the edge label, in the canvas's own units.
_STRIDE_Y = 90

#: Config fields a model may not write, **beyond** the resource references
#: `is_resource_field` already names — ADR-0025 §5 applied to permission sets.
#:
#: `allowed_actions` is the case that forced this. It is not a reference to a
#: resource, so `is_resource_field` says nothing about it, but it decides *what
#: a step may reach*: empty means every action the connection offers. So a model
#: that edits anything else about an `mcp` node and simply **omits** this field
#: hands it back at its blank default — silently widening a node the creator had
#: narrowed to `["read_email"]` into one that may call `send_email`. No malice
#: required, and nothing on screen would say it happened.
#:
#: `action_mode` travels with it for the same reason in one move: flipping
#: `single` to `full` turns "always this one action" into "whichever the model
#: picks", which is the same widening by another door.
#:
#: The platform's other guards do survive this — `riskiest()` still raises the
#: approval floor to the widened set, so a graph that gained `send_email` will
#: not confirm without a human approval step, and the runtime still gates the
#: call. This closes the door those guards are the backstop for: a creator's own
#: narrowing must not evaporate because a model rewrote the graph around it.
#:
#: Consequence worth stating: a model cannot narrow these by chat either, on a
#: new node or an existing one. That capability has never existed, and adding it
#: means deciding what a narrowing *is* (subset of what?) — a decision that
#: deserves its own change rather than arriving as a side effect of this one.
#: The Inspector's action dialog is where these are set.
_CREATOR_OWNED: dict[NodeType, frozenset[str]] = {
    NodeType.MCP: frozenset({"action_mode", "allowed_actions"}),
}

#: How close two nodes may be before `_placed` counts the spot as occupied.
#: Deliberately smaller than `_STRIDE_Y`: this is "these two would overlap", not
#: "these two look cramped", and pushing apart merely cramped nodes would undo
#: layouts creators arranged on purpose.
_TOO_CLOSE = 60

#: What a creator is told when the meta-agent could not write a valid graph for
#: their request. Their next move, and the reassurance that matters most here:
#: the canvas in front of them is untouched, because this call never wrote
#: anything anywhere.
_EXHAUSTED = (
    "Chưa sửa được workflow theo yêu cầu này sau {attempts} lần thử. "
    "Bạn thử nói lại cụ thể hơn giúp — nêu rõ bước nào và sửa gì. "
    "Workflow trên canvas vẫn giữ nguyên, không có gì bị mất."
)

#: Said when the request is empty. Refused before the first call: there is no
#: change to make, so no number of attempts produces one.
_BLANK = (
    'Chưa rõ bạn muốn sửa gì. Nói cụ thể giúp — ví dụ "thêm bước duyệt tay trước khi gửi mail", '
    '"bỏ bước tìm kiếm web", hay "câu hỏi cho người duyệt nói rõ số tiền".'
)

_REFINEMENT_PROMPT = """\
Bạn là Workflow Generator của một nền tảng dựng agent AI. Người tạo đã có sẵn một \
workflow và muốn sửa nó bằng một câu nói.

Workflow hiện tại, dạng JSON:
\"\"\"
{current}
\"\"\"

Người tạo yêu cầu sửa:
\"\"\"
{instruction}
\"\"\"

Các loại node được phép, kèm cổng ra của từng loại và các trường trong "config":
{catalogue}

Quy tắc bắt buộc:
- Chỉ sửa ĐÚNG phần người tạo vừa yêu cầu. Mọi node không được nhắc tới phải giữ nguyên \
từng chữ — cùng "id", cùng "type", cùng "config" — và giữ nguyên các cạnh không liên quan.
- Trả về TOÀN BỘ workflow sau khi sửa, không phải phần khác biệt.
- "id" của node và của cạnh không được trùng nhau. Cạnh mới thì đặt "id" mới chưa có ai dùng.
- Mỗi cạnh đi ra từ một cổng: "source_port" phải là một trong các cổng của loại node nguồn \
theo bảng trên. Một cổng chỉ được có tối đa một cạnh đi ra.
- Bỏ một node là xoá nó khỏi "nodes" VÀ xoá mọi cạnh chạm vào nó; nếu nó đang là \
"start_node_id" thì trỏ "start_node_id" sang node thay thế.
- Thêm một node là thêm vào "nodes" VÀ nối cạnh vào cho nó, để nó không bị cô lập.
- Đừng bận tâm "position" — hệ thống tự xếp chỗ, bạn ghi gì cũng bị bỏ qua.
- Đừng tự điền {resource_fields} — đó là file và kết nối của riêng người tạo, hệ thống \
giữ nguyên giá trị cũ và để trống cho node mới. Bạn ghi vào cũng bị bỏ qua.

Chỉ trả lời bằng một JSON object đúng khuôn của workflow hiện tại, không kèm giải thích.
"""


class BlankGraphInstructionError(ValueError):
    """The creator asked for an edit without saying what to change. Message is user-facing.

    Its own type rather than a generation failure, because nothing failed: no
    call was made and no attempt was spent. A caller that could not tell the two
    apart would answer "the meta-agent is having trouble, try again" to somebody
    who simply pressed send on an empty box.
    """


class GraphRefinementError(ValueError):
    """The meta-agent could not produce a valid workflow. `args[0]` is user-facing.

    Carries `reason` beside the message for the same split
    `SpecGenerationError` makes: the creator reads a sentence about what they
    can do next, and the validation error that actually happened is about a JSON
    document they have never seen.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


async def refine_workflow(
    *,
    current: WorkflowJSON,
    instruction: str,
    model_factory: ChatModelFactory,
) -> WorkflowJSON:
    """`current`, changed the way `instruction` asks, as a freshly validated `WorkflowJSON`.

    Returns a **new** graph and never touches the one it was given. That is what
    lets the canvas go on showing the current graph while the edit is attempted,
    and what makes a refusal cost nothing: there is no half-edited state to roll
    back, because nothing was edited.

    Nothing is persisted here either, and that is the caller's contract too —
    see the route in `src.api.workflow_routes`.
    """
    asked = instruction.strip()
    if not asked:
        raise BlankGraphInstructionError(_BLANK)
    return await write_document(
        build_prompt=lambda: _refinement_prompt(current=current, instruction=asked),
        parse=lambda reply: _graph_from(reply, current=current),
        model_factory=model_factory,
        exhausted=_EXHAUSTED,
        failure=lambda message, reason: GraphRefinementError(message, reason=reason),
        purpose=PlatformCallPurpose.REFINE_GRAPH,
    )


def _refinement_prompt(*, current: WorkflowJSON, instruction: str) -> str:
    """The whole current graph, the request, and the node catalogue.

    The graph goes in verbatim rather than summarised. It is 2–6 KB — smaller
    than the `AgentSpec` the V1 path already embeds whole — and a summary is a
    second description of the document the reply has to match, which is the
    thing that drifts.
    """
    return _REFINEMENT_PROMPT.format(
        current=json.dumps(current.model_dump(mode="json"), ensure_ascii=False, indent=2),
        instruction=instruction,
        catalogue=_catalogue_block(),
        resource_fields=", ".join(f'"{field}"' for field in sorted(_all_resource_fields())),
    )


def _catalogue_block() -> str:
    """Every node type with its ports and config fields, read off `NODE_REGISTRY`.

    Generated rather than written out, for the reason `SCHEMA_RULES` gives about
    the spec's rules: a prompt carrying its own copy of the eight types is a
    prompt that can fall behind ADR-0021 §2 without anything going red.
    """
    lines = []
    for node_type in NODE_REGISTRY:
        ports = ", ".join(outputs_of(node_type)) or "không có, đây là node kết thúc"
        declared = ", ".join(config_model_of(node_type).model_fields)
        fields = f"config có {declared}" if declared else "config rỗng"
        lines.append(f'- "{node_type.value}": cổng ra [{ports}]; {fields}')
    return "\n".join(lines)


def _all_resource_fields() -> set[str]:
    """Every field name across the eight types that addresses a creator's own resource."""
    return {
        field
        for node_type in NODE_REGISTRY
        for field in config_model_of(node_type).model_fields
        if is_resource_field(node_type, field)
    }


def _graph_from(reply: str, *, current: WorkflowJSON) -> WorkflowJSON:
    """The model's reply as a `WorkflowJSON`, with what it may not decide put back.

    Order matters and is the same order `fill._configured` uses: parse first so a
    malformed reply is retried rather than half-applied, then overwrite, then
    validate. Overwriting *before* validation is what lets a model return a new
    MCP node with no `tool_id` at all — the blank the creator has to fill is
    written in here, not demanded of the model.
    """
    document = json_object(reply)
    return restored_graph(WorkflowJSON.model_validate(document), current=current)


def restored_graph(proposed: WorkflowJSON, *, current: WorkflowJSON) -> WorkflowJSON:
    """`proposed` with everything the model does not own taken back from `current`.

    The module's enforcement core, public since ADR-0050: the meta-agent loop's
    `de_xuat_graph` tool runs every proposal through this exact function, so the
    one-shot path and the loop cannot drift apart on what a model may decide.
    """
    kept = {node.id: node for node in current.nodes}
    return proposed.model_copy(update={"nodes": _restored(proposed, kept)})


def _restored(proposed: WorkflowJSON, kept: dict[str, NodeInstance]) -> list[NodeInstance]:
    """Every proposed node with its resource fields and position taken back from `kept`."""
    placed: list[NodeInstance] = []
    for node in proposed.nodes:
        before = kept.get(node.id)
        matched = before if before is not None and before.type is node.type else None
        restored = node.model_copy(update={"config": _config_of(node, matched)})
        if matched is not None:
            placed.append(restored.model_copy(update={"position": matched.position}))
        else:
            placed.append(restored.model_copy(update={"position": _placed(restored, proposed, placed, kept)}))
    return placed


def _config_of(node: NodeInstance, matched: NodeInstance | None) -> dict[str, Any]:
    """`node`'s config, cut down to the fields its type declares, resource ones restored.

    Built key by key rather than patched, and that is what closes the gap a patch
    leaves open: a model that answers with a `tool_id` on a `human_approval` node
    is writing a resource reference into a type that has no resource field, so
    overwriting only the *declared* resource fields would let it through
    untouched. Keeping nothing but the declared fields means there is nowhere for
    an invented value to land, whatever type it was addressed to. Same reasoning
    as `fill._prose`, which keeps only what the model was asked for.

    `matched` is the node of the same id **and the same type**. A node whose type
    the edit changed is a different node wearing an old name, and carrying an
    `mcp` connection onto it would attach a creator's credential to a step they
    never pointed at it — so it gets blanks, like any other new node.

    A declared field the model left out comes back as its blank default, which is
    the shape the canvas's own `blankConfig` produces for a node just dropped out
    of the palette.
    """
    declared = config_model_of(node.type).model_fields
    # `mode="json"`, because a node's `config` is a stored JSON document and
    # nothing else in it is a Python object. Plain `model_dump()` hands back an
    # enum *member* for an enum-typed field, and `McpConfig.require_approval` is
    # the first of those — a `StrEnum` compares and serialises like its value, so
    # it would sit in a saved workflow unnoticed until something reached for a
    # `type()` rather than an `==`.
    blanks = config_model_of(node.type)().model_dump(mode="json")
    kept = dict(matched.config) if matched is not None else {}
    owned = _CREATOR_OWNED.get(node.type, frozenset())
    return {
        field: (
            kept.get(field, blanks[field])
            if is_resource_field(node.type, field) or field in owned
            else node.config.get(field, blanks[field])
        )
        for field in declared
    }


def _placed(
    node: NodeInstance,
    proposed: WorkflowJSON,
    settled: list[NodeInstance],
    kept: dict[str, NodeInstance],
) -> Position:
    """Where a node that did not exist before should sit.

    Beside whatever feeds it, because that is where a reader's eye already is:
    an inserted approval step appears to the right of the node it approves,
    rather than at an origin the creator then has to go and find. With nothing
    feeding it — a new entry point, or a node the model left for the creator to
    wire — it goes below everything, which is the one place guaranteed to be
    empty.

    `settled` is the nodes already placed in this same pass, so two new nodes
    hanging off one source do not land on top of each other.
    """
    known = {existing.id: existing for existing in settled} | kept
    upstream = [
        known[edge.source_node_id]
        for edge in proposed.edges
        if edge.target_node_id == node.id and edge.source_node_id in known
    ]
    if upstream:
        anchor = upstream[0]
        return _free(Position(x=anchor.position.x + _STRIDE_X, y=anchor.position.y), known.values())
    lowest = max((existing.position.y for existing in known.values()), default=0.0)
    return _free(Position(x=80.0, y=lowest + _STRIDE_Y), known.values())


def _free(wanted: Position, taken: Iterable[NodeInstance]) -> Position:
    """`wanted`, pushed down until nothing already placed is sitting on it."""
    occupied = [node.position for node in taken]
    spot = wanted
    while any(abs(spot.x - other.x) < _TOO_CLOSE and abs(spot.y - other.y) < _TOO_CLOSE for other in occupied):
        spot = Position(x=spot.x, y=spot.y + _STRIDE_Y)
    return spot
