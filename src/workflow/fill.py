"""Turning a template's placeholders into a workflow about somebody's business.

ADR-0025 §5 splits this by the kind of field, not by who is convenient to ask:

- a field that **addresses a resource** — `KnowledgeSearch.source_ids`,
  `MCP.tool_id`/`action` — is mapped from the spec by rule. A model asked for
  one produces a plausible key that resolves to nothing, and the creator finds
  out at run time, in a workflow that looks correctly wired.
- a field that is **business wording** — `AgentLlm.prompt`, `IfElse.condition`,
  `HumanApproval.message_template` — is written by the model in one call. No
  rule turns *"trả lời khiếu nại trong một ngày làm việc"* into a usable prompt.

`AgentLlm.model` belongs to neither and is a platform default, the same instinct
ADR-0022 §3 applied to `metadata.budget`: a technical choice the spec was
deliberately kept free of, filled with something safe that the creator can
change on the canvas.

Like `src.solution.designer`, this never raises on what the model said. A reply
that cannot be read leaves the prose fields blank, and a blank field is the
ordinary state of a fresh draft — the canvas marks it, confirmation refuses it,
and the creator writes it. A parse error surfacing here would be the platform's
flakiness arriving as *"no workflow for you."*
"""

from collections.abc import Mapping, Sequence
from typing import Any

from src.db.models import PlatformCallPurpose
from src.evals.model_call import ask
from src.evals.model_json import UnreadableModelReplyError, json_object
from src.models.model_catalog import default_node_model
from src.models.node_registry import NodeType, is_prose_field, prose_fields
from src.models.solution_spec import AgentSolutionSpecification, DataSource, DataSourceKind
from src.models.workflow import NodeInstance, WorkflowJSON
from src.services.llm import ChatModelFactory, document_model
from src.workflow.effects import without_diacritics
from src.workflow.templates import TemplateSpec
from src.workflow.tools import TOOL_DEFINITIONS, ToolDefinition, ToolFamily

_FILL_PROMPT = """\
Bạn là Workflow Generator của một nền tảng dựng agent AI. Bộ khung workflow đã được chọn \
xong; việc của bạn chỉ là viết phần câu chữ nghiệp vụ cho từng node.

Bài toán nghiệp vụ:
{problem}

Bộ khung đã chọn — {template_name}: {template_description}

Toàn bộ workflow, để bạn biết mỗi node nằm ở đâu trong luồng:

{graph}

Các node đang chờ bạn viết:

{nodes}

Quy tắc:
- Viết bằng tiếng Việt, bằng đúng ngôn ngữ nghiệp vụ của người tạo ở trên.
- "prompt": nói rõ node đó phải làm gì với dữ liệu nó nhận được, và những gì tuyệt đối không được làm.
- Prompt phải khớp với vị trí của node trong luồng ở trên: node nhận gì từ node trước, \
và node sau nó cần gì để làm tiếp. Node đứng trước một nhánh rẽ phải nói rõ nó kết luận ra cái gì \
để nhánh đó rẽ được.
- "condition": một mệnh đề đúng/sai, kiểm tra được trên đúng thứ mà node trước nó vừa kết luận, \
không phải một câu mô tả.
- "message_template": câu người duyệt sẽ đọc khi workflow dừng lại chờ họ quyết định — nêu đủ \
thông tin để họ quyết được mà không phải đi tra chỗ khác.
- Không viết gì về file, tool hay kết nối cụ thể — hệ thống đã tự nối những thứ đó rồi.

Chỉ trả lời bằng một JSON object, không kèm giải thích:
{skeleton}
"""


async def fill_config(
    *,
    template: TemplateSpec,
    spec: AgentSolutionSpecification,
    model_factory: ChatModelFactory,
    model_allowlist: Sequence[str],
) -> WorkflowJSON:
    """`template`'s skeleton, configured for `spec`, as a draft `WorkflowJSON`.

    One model call for the whole graph rather than one per node: a prompt and
    the condition that branches after it are two halves of one piece of
    reasoning, and a model shown only one node at a time writes them as if the
    other did not exist. Since ADR-0041 the call is also *shown* that shape —
    `_graph_block` puts the wiring in the prompt — because the argument above was
    only ever true of a model that can see which node feeds which.

    The shape never changes here. Nodes, edges and `start_node_id` come out as
    the template declared them — that is what keeps `metadata.template_id`
    meaningful (ADR-0025 §1), and it is why this function returns a document
    rather than a graph it assembled. Composing a graph freehand is
    `src.workflow.composition`'s job, and the two are separate for that reason.

    `model_allowlist` is the admin's, and it reaches this far down for one field:
    the model a drafted `AgentLlm` node is assigned. A default that ignored it
    would write a workflow naming a model the platform's own runtime refuses.
    """
    skeleton = template.skeleton()
    authored = await _authored_prose(template=template, spec=spec, model_factory=model_factory)
    return skeleton.model_copy(
        update={
            "nodes": [
                _configured(node, spec, authored.get(node.id, {}), model_allowlist=model_allowlist)
                for node in skeleton.nodes
            ]
        }
    )


def _configured(
    node: NodeInstance,
    spec: AgentSolutionSpecification,
    prose: Mapping[str, str],
    *,
    model_allowlist: Sequence[str],
) -> NodeInstance:
    """One node with its placeholders filled: rules first, then the model's words.

    Rules first and prose second is not an ordering detail — `_prose` only ever
    writes prose keys, so a model that answered with a `source_ids` of its own
    has nowhere to put it.
    """
    config = dict(node.config) | resource_config(node, spec, model_allowlist=model_allowlist) | _prose(node, prose)
    return node.model_copy(update={"config": config})


def resource_config(
    node: NodeInstance, spec: AgentSolutionSpecification, *, model_allowlist: Sequence[str]
) -> dict[str, Any]:
    """The fields of `node` that the spec already answers, mapped directly.

    Every value here traces back to something the creator actually attached or
    actually said. Nothing in this function guesses.

    Public since ADR-0041, because the Workflow Composer fills exactly the same
    fields the same way. A model drawing a graph freehand is no more entitled to
    invent a Knowledge Base id than one filling a template's placeholder was, so
    both paths route through this one function rather than through two rules
    that could come to disagree about what a `KnowledgeSearch` node searches.
    """
    if node.type is NodeType.KNOWLEDGE_SEARCH:
        return {"source_ids": _knowledge_base_refs(spec)}
    if node.type is NodeType.MCP:
        return _mcp_reference(spec)
    if node.type is NodeType.AGENT_LLM:
        return {"model": default_node_model(model_allowlist)}
    return {}


def _knowledge_base_refs(spec: AgentSolutionSpecification) -> list[str]:
    """The Knowledge Base ids a `KnowledgeSearch` node may point at — ADR-0043 §1.

    Only `uploaded_file` sources carry one: their `ref` is a shelf the creator
    picked after the interview, on the spec screen or the canvas. A
    `connected_account`'s `ref` is a connection id (`_mcp_reference` reads it
    for exactly that), and it has never named a Knowledge Base — folding it in
    here would hand a `KnowledgeSearch` node a source id RAG-05's own validator
    (`src.workflow.knowledge_bases.ensure_knowledge_bases_owned`) can never
    resolve.
    """
    return [
        source.ref for source in spec.data if source.kind is DataSourceKind.UPLOADED_FILE and source.ref is not None
    ]


def _prose(node: NodeInstance, authored: Mapping[str, str]) -> dict[str, str]:
    """What the model wrote for `node`, less anything it was not asked to write.

    Filtered against `is_prose_field` for `node`'s own type, so a model that
    answered with a field belonging to a different node type — or with a
    `source_ids` it invented — has nowhere to put it. That predicate reads the
    `PROSE` marker off the field declarations rather than a list kept here,
    because the Workflow Canvas (#44) asks the same question for its own reasons
    (which inputs get a text area) and two lists would answer it differently the
    first time a ninth prose field is added.

    A blank answer is dropped rather than written, leaving the template's
    placeholder, which the canvas shows as unfilled instead of as an empty
    prompt somebody chose.
    """
    return {
        name: value.strip()
        for name, value in authored.items()
        if is_prose_field(node.type, name) and isinstance(value, str) and value.strip()
    }


def _mcp_reference(spec: AgentSolutionSpecification) -> dict[str, str]:
    """The connection an `MCP` node acts through — and deliberately not the action.

    `tool_id` is derivable: the creator attached an account, the registry knows
    which connection that is, and getting it wrong is visible. Resolved against
    the real registry (`src.workflow.tools`) since #47 — before it existed this
    wrote the creator's own `DataSource.ref`, which now resolves to nothing, and
    a field the canvas shows as *filled* while the runtime cannot resolve it is
    worse than a blank the canvas rings.

    `action` is **not** derivable and is left blank. The only signal a spec
    carries is whether the creator named an unrecoverable effect
    (`src.workflow.effects`), and that is one bit against a connection with
    several actions: a creator who wrote *"xoá tệp cũ"* would be bound to
    `send_email` purely because both are high risk, which is the exact failure
    ADR-0025 §5 splits rule-mapped fields from model-written ones to avoid — a
    plausible reference to the wrong thing. Choosing between `send_email` and
    `create_draft` is a decision a person should make, and leaving it blank is
    what makes the canvas ask them and confirmation refuse until they answer.
    """
    tool = _connected_tool(spec.data)
    return {"tool_id": tool.id if tool is not None else ""}


def _connected_tool(data: Sequence[DataSource]) -> ToolDefinition | None:
    """The registered connection one of the creator's connected accounts refers to.

    Matched on the tool's own words appearing in the account the creator
    attached, both sides diacritic-stripped by the one normaliser this platform
    has (`src.workflow.effects`), so *"Gmail của phòng CSKH"* finds the
    `google_workspace` connection and an unrelated account finds nothing.

    A word match rather than the first connection of the right *kind*, which is
    what this did before there was anything to match against: pointing an `MCP`
    node at whichever account happened to be listed first would wire a workflow
    to a system the creator never mentioned, and it would look deliberate.
    """
    mcp_tools = [tool for tool in TOOL_DEFINITIONS.all() if tool.family is ToolFamily.MCP]
    mentioned = connected_tool_ids(data, [(tool.id, tool.name) for tool in mcp_tools])
    return next((tool for tool in mcp_tools if tool.id in mentioned), None)


def connected_tool_ids(data: Sequence[DataSource], tools: Sequence[tuple[str, str]]) -> list[str]:
    """The ids among `tools` (id, name) that one of the spec's connected accounts names.

    The one word-matcher for "which connection did the creator mean", shared by
    `_connected_tool` (which takes the first) and the Workflow Composer (which
    offers the model *only* these). Measured 2026-08-30: a composer shown the
    creator's whole catalog drew an `mcp` node to "read" a policy shelf in 8 of
    16 graphs whose spec named no account at all.
    """
    # Whole tokens, not substrings: "don" (from a registered "Đơn hàng" server)
    # is inside "kinh doanh", and a substring match offered that server's
    # `cancel_order` to a spec that only ever named the department's Gmail.
    sources = [
        set(without_diacritics(f"{source.name} {source.ref or ''}").replace("_", " ").split())
        for source in data
        if source.kind is DataSourceKind.CONNECTED_ACCOUNT
    ]
    return [tool_id for tool_id, name in tools if any(_names_tool(tokens, tool_id, name) for tokens in sources)]


def _names_tool(source_tokens: set[str], tool_id: str, name: str) -> bool:
    """Whether one connected-account source names this tool: by its id, or by its whole name.

    The whole name, because a registered server called *"ERP Kinh doanh"* shares
    two of its three words with *"Gmail của phòng Kinh doanh"* — the department
    is in both — and a one-word match offered that server's `cancel_order` to a
    spec that only ever mentioned the department's mailbox. "Gmail" and "Google
    Drive" are still found by their id and by their name alike.
    """
    id_tokens = {word for word in without_diacritics(tool_id).replace("-", " ").replace("_", " ").split() if len(word) > 2 and not word.isdigit() and word != "registered"}
    name_tokens = {word for word in without_diacritics(name).replace("_", " ").split() if len(word) > 1}
    return bool(id_tokens & source_tokens) or (bool(name_tokens) and name_tokens <= source_tokens)


async def _authored_prose(
    *,
    template: TemplateSpec,
    spec: AgentSolutionSpecification,
    model_factory: ChatModelFactory,
) -> dict[str, dict[str, str]]:
    """The model's wording for every node that needs some, keyed by node id.

    Empty when there is nothing to ask about, and empty when the reply could not
    be read — the two cases are the same to every caller: the template's
    placeholders stand.
    """
    waiting = _nodes_waiting_for_prose(template)
    if not waiting:
        return {}
    reply = await ask(
        model_factory(document_model()),
        _FILL_PROMPT.format(
            problem=problem_block(spec),
            template_name=template.name,
            template_description=template.description,
            graph=_graph_block(template),
            nodes=_nodes_block(waiting),
            skeleton=_skeleton_block(waiting),
        ),
        purpose=PlatformCallPurpose.FILL_TEMPLATE,
    )
    try:
        document = json_object(reply)
    except UnreadableModelReplyError:
        return {}
    nodes = document.get("nodes")
    if not isinstance(nodes, Mapping):
        return {}
    return {
        node_id: {key: value for key, value in written.items() if isinstance(value, str)}
        for node_id, written in nodes.items()
        if isinstance(node_id, str) and isinstance(written, Mapping)
    }


def _nodes_waiting_for_prose(template: TemplateSpec) -> list[tuple[NodeInstance, tuple[str, ...]]]:
    """Each node that has prose fields, paired with the fields it needs written."""
    waiting = []
    for node in template.nodes:
        fields = prose_fields(node.type)
        if fields:
            waiting.append((node, fields))
    return waiting


def problem_block(spec: AgentSolutionSpecification) -> str:
    """The whole confirmed spec, as any prompt that writes *about* it needs to read it.

    All seven fields, and that is the change ADR-0041 made here. It used to carry
    five: `data` and `success_criteria` were left out, on the reasoning that
    neither decides a graph's shape. That reasoning was about *choosing a
    template* (`selection._problem_block`, which still holds it) and it does not
    survive the move to writing node prompts — a node told to search documents it
    has never been told the names of writes "tìm trong tài liệu", and a node with
    no idea what the creator will judge the answer by writes a prompt that meets
    no criterion in particular.

    Shared with the Workflow Composer, which reads the same seven fields for the
    same reason: one description of the problem, so a graph and the wording
    inside it cannot be built from two different readings of it.
    """
    data = "; ".join(f"{source.name} ({source.kind.value})" for source in spec.data)
    criteria = "; ".join(f"{criterion.text} [{criterion.category.value}]" for criterion in spec.success_criteria)
    return "\n".join(
        [
            f"- Mục tiêu: {spec.goal}",
            f"- Người dùng: {'; '.join(spec.users)}",
            f"- Dữ liệu agent đọc: {data}",
            f"- Hành động agent phải làm: {'; '.join(spec.actions)}",
            f"- Chạy xong người dùng nhận được: {'; '.join(spec.expected_outcome)}",
            f"- Trả lời thế nào là đúng: {criteria}",
            f"- Tuyệt đối không được: {'; '.join(spec.prohibitions)}",
        ]
    )


def _graph_block(template: TemplateSpec) -> str:
    """The skeleton's wiring, so a node's prompt can be written for where it sits.

    The gap ADR-0041 closed. This call's own docstring has always argued that a
    prompt and the condition branching after it are one piece of reasoning — and
    then handed the model a flat list of node ids with no way to tell which node
    branched after which. What it wrote was wording for seven nodes that each
    believed they were the only one.

    Written as lines rather than as the JSON document: the model is not editing
    this graph and cannot change it, so what it needs is the reading order, not a
    document to copy. (`src.workflow.refinement`, which *does* edit, embeds the
    JSON verbatim for exactly the opposite reason.)
    """
    lines = [f"Bắt đầu ở node: {template.start_node_id}"]
    lines.extend(f"- node {node.id} (loại {node.type.value})" for node in template.nodes)
    lines.extend(f"- {edge.source_node_id} --[{edge.source_port}]--> {edge.target_node_id}" for edge in template.edges)
    return "\n".join(lines)


def _nodes_block(waiting: Sequence[tuple[NodeInstance, tuple[str, ...]]]) -> str:
    """Every node needing wording, as its id, type and the fields to write."""
    return "\n".join(f"- {node.id} (loại {node.type.value}): cần viết {', '.join(fields)}" for node, fields in waiting)


def _skeleton_block(waiting: Sequence[tuple[NodeInstance, tuple[str, ...]]]) -> str:
    """The exact JSON shape asked for, written out with this graph's own node ids.

    A shape shown with real ids rather than described in prose, because a model
    given `{"nodes": {"<node_id>": ...}}` answers with a node called
    `"<node_id>"` often enough to matter.
    """
    body = ",\n".join(
        '    "{id}": {{{fields}}}'.format(id=node.id, fields=", ".join(f'"{field}": "..."' for field in fields))
        for node, fields in waiting
    )
    return '{\n  "nodes": {\n' + body + "\n  }\n}"
