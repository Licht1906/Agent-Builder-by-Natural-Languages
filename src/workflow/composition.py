"""Drawing a workflow for a problem no template fits — the Composer, ADR-0041.

ADR-0025 §1 settled that the Generator *chooses* a template rather than drawing a
graph, and gave a good reason: an LLM drawing freehand produces graphs that do
not run. That reason has since stopped being true of this codebase, and ADR-0041
records why. `src.workflow.validation` now decides whether a graph is one the
runtime could honestly execute — every rule, on any document, from any author —
and `src.services.model_document` already re-asks a model with the complaint in
hand. Put together they are a composer: the model proposes a graph, the validator
answers in sentences the model can act on, and nothing invalid reaches a creator
because nothing invalid gets past the loop.

What that buys is the thing the catalog could not: a creator whose business is
not one of eight recognised shapes gets a workflow *about their business* rather
than the nearest listed neighbour. The templates stay, and they stay useful —
they are now what a creator picks deliberately from a gallery, which is what a
template library was always for. Neither is a fallback for the other's failure:
see `src.workflow.generator` for which door a request comes in by.

**Three things are not the model's to decide**, and each is enforced here rather
than asked for in the prompt:

- **resource references** — `source_ids`, `tool_id`, `action`. Filled by rule
  from the spec (`fill.resource_config`) or left blank, exactly as they are on
  the template path. ADR-0025 §5's argument is untouched by ADR-0041: a model
  that invents a plausible blob key produces a graph that looks wired and
  resolves to nothing.
- **positions** — assigned by `src.workflow.layout`, because a model asked for
  coordinates writes round numbers that overlap.
- **the safety invariant** — a spec whose actions really send or delete
  something cannot come back as a graph with nobody standing in front of the
  side effect (ADR-0023 §5). The template path got this free from the shapes it
  chose between; freehand has to check it, and does, as a rejection the model is
  told about and asked to fix.

Everything else — how many nodes, of which types, wired which way, and every
word of prose in them — is the model's work, checked on the way in.
"""

import re
from collections.abc import Sequence
from dataclasses import replace
from enum import Enum
from typing import Any, get_origin

from src.db.models import PlatformCallPurpose
from src.evals.model_json import json_object
from src.metaagent.catalog import catalog_lines
from src.metaagent.loop import CatalogTool
from src.models.agent_spec import SYSTEM_LIMITS
from src.models.model_catalog import (
    ASSIGNABLE_MODELS,
    ModelInfo,
    ModelTier,
    default_node_model,
    undominated,
)
from src.models.node_registry import (
    NODE_REGISTRY,
    NodeType,
    config_model_of,
    is_prose_field,
    is_resource_field,
    outputs_of,
)
from src.models.solution_spec import AgentSolutionSpecification, DataSourceKind
from src.models.workflow import Edge, NodeInstance, WorkflowJSON
from src.services.llm import ChatModelFactory
from src.services.model_document import DocumentRejectedError, write_document
from src.workflow.effects import has_high_risk_action, without_diacritics
from src.workflow.fill import connected_tool_ids, problem_block, resource_config
from src.workflow.layout import laid_out
from src.workflow.validation import raw_payload_as_answer, structural_problems

#: The most nodes a composed graph may hold. Still `SYSTEM_LIMITS.max_steps`,
#: because a platform with two answers to "how big may one agent be" enforces
#: whichever one the creator happened to route through. What changed is the
#: number: a V2 node is not a V1 step — an `end` per branch, an `if_else`, a
#: `collect`, a `human_approval` are all shape rather than work — so the shared
#: ceiling was quietly buying a V2 creator a third of the agent it bought a V1
#: one. `SYSTEM_LIMITS` carries the whole argument and the arithmetic.
MAX_NODES = SYSTEM_LIMITS.max_steps

#: What a creator is told when the meta-agent could not draw a valid graph. Their
#: next move is about the spec, because that is the only input they control —
#: and it is a real move: the sharper the goal and actions, the smaller the graph
#: the composer has to invent.
_EXHAUSTED = (
    "Chưa dựng được workflow cho bài toán này sau {attempts} lần thử. "
    "Bạn thử lại giúp — nếu vẫn vậy, quay lại màn hình đặc tả và viết rõ hơn phần "
    "agent phải làm những việc gì, theo thứ tự nào, rồi thử lại. "
    "Hoặc chọn một mẫu workflow có sẵn rồi sửa tiếp trên canvas."
)

#: The brief, and **the problem statement stays at the top of it** — which is
#: worth a note, because the obvious cost optimisation is to move it to the
#: bottom and it does not work.
#:
#: OpenAI caches a prompt by its prefix at a quarter of the price. Everything
#: below the problem block here is invariant — the node catalogue, the model
#: list, all four principles, the field contract — about 2,360 of 2,545 tokens,
#: **93%**, which would be cached for every creator on the platform instead of
#: for nobody. It was tried. Same model, same four specs, three passes each:
#:
#:     bài toán ở đầu (nguyên trạng)   12/12 hợp lệ   1.33 lần thử   $0.00256/graph
#:     bài toán ở cuối (đã thử)         9/12 hợp lệ   1.50 lần thử   $0.00317/graph
#:
#: The caching worked perfectly — 82% of input tokens came back cached — and it
#: still lost, because one of the four specs went from 3/3 valid to 0/3. A
#: composition that fails three times costs more than the cache saves and hands
#: the creator a template instead of their own workflow.
#:
#: The likely reason: the rules point at the problem the whole way through —
#: *"theo đúng thứ tự nghiệp vụ"*, *"bằng đúng ngôn ngữ nghiệp vụ của người
#: tạo"* — and with the problem 2,300 tokens further down, the model reads every
#: rule before it knows what any of them is about.
_COMPOSITION_PROMPT = """\
Bạn là Workflow Composer của một nền tảng dựng agent AI. Việc của bạn: đọc bài toán \
nghiệp vụ dưới đây rồi **tự vẽ** một workflow giải đúng bài toán đó, viết ra dưới dạng JSON.

Bài toán nghiệp vụ:
{problem}

Các loại node được phép dùng — không có loại nào khác:

{catalogue}

Model được phép gán cho "model" của node agent_llm:
{models}

Các kết nối và action người tạo có thể gắn vào node "mcp" (người tạo gắn sau, bạn không điền) — dấu * là tham số bắt buộc:
{tools}

Quy tắc bắt buộc — vi phạm là workflow bị từ chối và bạn phải viết lại:

Hình dạng graph:
- Vẽ đúng những gì bài toán cần, theo đúng thứ tự nghiệp vụ. Mỗi node làm một việc. Nhiều nhất \
{max_nodes} node; ít mà đủ thì tốt hơn nhiều mà thừa.
- "start_node_id" phải là id của một node có thật trong "nodes". Mọi nhánh đi từ node bắt đầu đều \
phải tới được một node loại "end". Mỗi cổng ra của mỗi node chỉ được nối ĐÚNG MỘT cạnh, và mọi cổng \
đều phải nối đi đâu đó ("if_else" và "collect" có hai cổng thì cả hai đều phải có cạnh). "source_port" \
của cạnh phải là một cổng có thật của loại node nguồn, theo bảng trên. Chỉ node "while" mới được tạo \
vòng lặp, và chỉ qua cổng "loop" của chính nó.
- Đừng tự điền {resource_fields} — đó là file và kết nối của riêng người tạo, hệ thống tự nối; bạn ghi \
vào cũng bị bỏ qua. Đừng bận tâm "position" — hệ thống tự xếp chỗ.

Bốn nguyên tắc — mọi quy tắc khác đều là hệ quả của chúng:
1. MỘT LƯỢT CHAT LÀ MỘT LẦN CHẠY từ node bắt đầu tới "end"; không node nào chờ người dùng giữa \
chừng. Hỏi người dùng = đi tới "end", và câu hỏi là lời của node đứng ngay trước "end". "while" chỉ lặp \
việc agent tự làm (tìm lại, sửa lại bản nháp) với "max_iterations" nhỏ — không bao giờ "cho tới khi \
người dùng cung cấp đủ", không bao giờ nối cổng "rejected" của duyệt quay lại; người dùng chỉ cần \
nhắn tiếp ở lượt sau.
2. GRAPH ĐI THEO SCHEMA CỦA ACTION, không theo văn xuôi của bài toán. Node "collect" CHỈ tồn tại khi \
graph có một node "mcp" mà tham số bắt buộc của action đó do người dùng cung cấp: "action_node" trỏ tới \
node mcp đó, cổng "incomplete" → "end", cổng "complete" → bước tiếp theo. Vị trí: "collect" đứng NGAY TRƯỚC \
node mcp (sau vòng soát, sau các node soạn). Ngoại lệ duy nhất: nếu trên đường tới mcp có "human_approval" \
thì "collect" phải đứng trước cả node soạn thảo và trước "human_approval" — để không soạn thứ không gửi \
được, và để người duyệt đọc được bản soạn chứ không đọc một danh sách tham số. Nền tảng tự đọc danh sách \
tham số bắt buộc của action người tạo gắn, tự hỏi đúng thứ còn thiếu bằng lời người dùng hiểu, và tự đi \
qua không tốn gì khi action không có tham số bắt buộc — nên KHÔNG viết node "agent_llm" để thu thập, \
KHÔNG viết "if_else" "đã đủ thông tin?", KHÔNG kể tên tham số ở bất cứ prompt hay condition nào. Tham số \
mà một node "agent_llm" đứng sau "collect" soạn ra (ví dụ "subject" và "body" của thư, "content" của tài \
liệu) thì ghi vào "agent_writes" để không hỏi người dùng — chỉ được ghi tên tham số có trong bảng action \
ở trên, đúng chính tả. Quy tắc chia: nội dung agent SOẠN ("subject", "body", "content") thì nằm trong \
"agent_writes"; thứ người dùng phải QUYẾT (địa chỉ nhận "to", tên tài liệu "title", mã "file_id", \
"message_id") thì KHÔNG — collect sẽ hỏi. Ví dụ: gửi thư → "agent_writes": ["subject", "body"]; tạo tài \
liệu → "agent_writes": ["content"]. Tham số tuỳ chọn (không dấu *) không bao giờ là lý do hỏi hay dừng. Thứ action \
không có tham số để nhận (tên hay email người gửi, CC không ai yêu cầu, trường "xác nhận") không tồn \
tại — tài khoản thực hiện action là tài khoản đã nối của chính người tạo. Bài toán KHÔNG có tài khoản \
kết nối thì graph KHÔNG có "mcp" và KHÔNG có "collect": câu người dùng gõ chính là đầu vào của lượt chạy, \
không cần node nào để nhận nó.
3. MỖI NODE CHỈ NÓI ĐIỀU CÓ TRONG ĐẦU VÀO CỦA NÓ. Kết quả "mcp" là dữ liệu thô: người đọc là con \
người thì đặt một "agent_llm" ngay sau để kể lại ("mcp" → "end" là trả lời bằng JSON). Node kể lại đó \
chỉ được nói những gì có trong kết quả công cụ — mã, tên, liên kết chép NGUYÊN VĂN; KHÔNG THÊM (không \
tự chế liên kết hay mã ví dụ; kết quả không có liên kết thì đưa mã), KHÔNG BỚT (không tự lọc theo điều \
kiện mà action không áp — "tuần trước", "của anh Nam" — mà nói rõ điều kiện đó chưa được kiểm); kết \
quả rỗng thì nói là rỗng. Prompt của node đó phải nói đúng những điều này.
4. BẢNG DỮ LIỆU → NODE, HIỆU ỨNG → DUYỆT. Tài liệu đã tải lên ("kind" uploaded_file/document, có \
"ref") → "knowledge_search". Tài khoản đã nối ("kind" connected_account: Gmail, Drive…) → "mcp", action \
read-only để đọc (search, recent, fetch, get_profile), action WRITE để ghi — không bao giờ \
"knowledge_search" cho một tài khoản, node đó không có tài liệu để tìm. Action "(read-only)" không \
cần "human_approval"; action "(WRITE, risk high)" BẮT BUỘC có "human_approval" ngay trước và chỉ đi \
tiếp từ cổng "approved"; risk medium KHÔNG cần "human_approval" trừ khi bài toán nói rõ phải có người \
duyệt. {approval_rule} "human_approval" chỉ đứng ngay trước \
một node "mcp" WRITE; graph không có "mcp" thì không có "human_approval". Cổng "rejected" phải dẫn tới \
một node "agent_llm" nói rõ việc chưa được làm và vì sao, rồi mới tới "end" — không bao giờ nối \
"rejected" thẳng vào "end". Các node đọc ("knowledge_search", "web_search") chỉ nạp bằng chứng, nối \
tiếp nhau trên một đường; MỘT node "agent_llm" đứng sau tất cả node đọc để trả lời — không vẽ một \
"agent_llm" riêng cho mỗi nguồn, không vẽ "if_else" "có tìm thấy không" (node trả lời tự nói khi không \
có), không vẽ node đọc nào không có cạnh vào. NGOẠI LỆ, và BẮT BUỘC khi graph có "web_search": đặt NGAY \
TRƯỚC mỗi node "web_search" một node "agent_llm" làm NODE TRÍCH. Prompt của node trích nói: đọc câu người \
dùng vừa gõ, lấy ra đúng tên riêng cần tra trên web (tên công ty, tổ chức, đối tác, sản phẩm) và CHỈ IN RA \
cụm từ tìm kiếm đó trên MỘT dòng — không viết thành câu, không giải thích, không thêm nhãn hay dấu nháy; câu \
người dùng không nêu tên riêng nào thì in ra chủ đề chính bằng vài từ. Node "web_search" tìm đúng theo dòng \
đó, nên câu dài của người dùng ("tuần sau em gặp <công ty> lần đầu, họ hỏi mua 20 máy bơm") không còn bị đem \
đi tìm nguyên câu. Node trích KHÔNG phải node trả lời: vẫn chỉ MỘT node "agent_llm" đứng sau tất cả node đọc \
để trả lời. "knowledge_search" thì KHÔNG có node trích — node đó tra tài liệu bằng chính lời người dùng.
5. NGƯỠNG → "if_else", TỰ SOÁT → "while". Bài toán nêu một ngưỡng hay bậc theo con số (số ngày quá hạn, \
số tiền, tỷ lệ phần trăm) thì vẽ "if_else" với "condition" nêu ĐÚNG ngưỡng đó bằng lời nghiệp vụ, ví dụ \
"Khoản nợ đã quá hạn từ 60 ngày trở lên, tức thuộc bậc cảnh báo"; đặt ngay trước nó một "agent_llm" nêu \
rõ con số đang xét để bộ định tuyến có gì mà xét; hai cổng dẫn tới hai node soạn khác nhau (mỗi bậc một \
node "agent_llm" với prompt riêng), rồi hai node đó có thể cùng đổ vào một "human_approval". MỖI ngưỡng \
bài toán nêu là MỘT node "if_else" — KHÔNG gộp các bậc vào một prompt "soạn đúng giọng của bậc đã xác định". Bài toán nói "tự \
soát lại / sửa tới khi đủ" thì vẽ "while": cổng "loop" → "agent_llm" soạn hoặc sửa → quay về chính node \
"while"; cổng "exit" → bước tiếp; "condition" gọi tên tiêu chí còn thiếu, ví dụ "bản thảo còn thiếu một \
trong ba mục: A, B, C — hoặc chưa có bản thảo nào"; "max_iterations" 2–3. Không thay "while" bằng hai \
node soạn–soát nối thẳng: đó không phải vòng lặp.

Hợp đồng của từng trường:
- "message_template" của "human_approval" và mọi "prompt" là văn bản thuần, KHÔNG có cú pháp thay \
thế: hệ thống in nguyên văn "message_template" rồi nối câu trả lời của node ngay trước vào bên dưới; \
"prompt" thì node tự đọc hội thoại. Viết một câu ngắn nói người duyệt đang duyệt cái gì ("Đây là email \
sẽ gửi đi, bạn đồng ý gửi không?"); TUYỆT ĐỐI không viết "{{{{...}}}}", "{{to}}", "[nội dung]" — người \
đọc sẽ thấy đúng mấy dấu ngoặc đó.
- "prompt" nói rõ node đó nhận gì, phải làm gì, và tuyệt đối không được làm gì. "condition" là một \
mệnh đề đúng/sai kiểm tra được trên đúng thứ mà node trước nó vừa kết luận — một câu tiếng Việt về \
nghiệp vụ, KHÔNG nhắc tên node, KHÔNG viết kiểu mã ("==", "true", ".found", "kết quả node X là 'Đủ'"). \
Viết cả hai bằng tiếng Việt, bằng đúng ngôn ngữ nghiệp vụ của người tạo ở trên.
- "model": **danh sách model ở trên xếp từ rẻ đến đắt. Mặc định lấy model ĐẦU DANH SÁCH cho MỌI \
node.** Chỉ đi xuống dưới khi node đó thực sự cần suy luận nhiều bước trên văn bản dài — và khi đi \
xuống thì lấy model đắt hơn ở ngay bên dưới chứ không nhảy xuống cuối, rồi viết một câu trong \
"prompt" của node nói vì sao cần. Node phân loại, node trích xuất, node kể lại kết quả tool, node \
soạn một đoạn thư ngắn: model đầu danh sách, không có ngoại lệ. Người tạo đổi được trên canvas bất \
cứ lúc nào; graph này chạy hàng nghìn lượt còn bạn chỉ vẽ nó một lần.

Chỉ trả lời bằng một JSON object đúng khuôn dưới đây, không kèm giải thích:
{skeleton}
"""

#: The `MCP` sentence, in the version that applies to this spec. Two sentences
#: rather than one always-on rule, because they are enforced differently: the
#: first is a refusal this module makes and hands back, and the second is advice
#: the creator may overrule on the canvas. A prompt that stated the strict rule
#: unconditionally would push an approval step into workflows whose only action
#: is reading an inbox — which is a stricter platform than any ADR asked for.
_APPROVAL_REQUIRED = (
    "Bài toán này có hành động gây hậu quả không thể hoàn tác, nên MỌI đường đi tới một node "
    '"mcp" đều BẮT BUỘC phải đi qua một node "human_approval" trước, và chỉ được đi tiếp từ '
    'cổng "approved" của node đó.'
)
_APPROVAL_ADVISED = (
    'Nếu hành động đó có thể gây hậu quả khó hoàn tác, hãy đặt một node "human_approval" ngay trước node "mcp" đó. '
    'Không có node "mcp" nào thì không có "human_approval" nào.'
)

_SKELETON = """\
{
  "start_node_id": "...",
  "nodes": [
    {"id": "...", "type": "...", "config": {}}
  ],
  "edges": [
    {"id": "...", "source_node_id": "...", "source_port": "...", "target_node_id": "..."}
  ]
}"""


class GraphCompositionError(ValueError):
    """The meta-agent could not draw a valid graph. `args[0]` is user-facing.

    Carries `reason` beside the message, the split `SpecGenerationError` and
    `GraphRefinementError` both make: the creator reads a sentence about what
    they can do next, and the validator's complaint is about a JSON document
    they have never seen.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


async def compose_workflow(
    *,
    spec: AgentSolutionSpecification,
    model_factory: ChatModelFactory,
    model_allowlist: Sequence[str],
    catalog: tuple[CatalogTool, ...] = (),
) -> WorkflowJSON:
    """A workflow drawn for `spec` from the eight node types, or a refusal.

    One model call per attempt, writing the shape *and* the wording together —
    unlike the template path, which is two calls because choosing a shape and
    filling one are genuinely two decisions there. Here they are not: a node
    exists in this graph because of what it will be asked to do, and splitting
    that in half would mean writing prompts for a graph that had already been
    drawn without them in mind.

    `metadata.template_id` comes back `None`, and that is the honest record of
    what happened (ADR-0022 §4): no template was used. The version-compare
    dashboard reads it, and "composed" is a real answer for it to show rather
    than a template id nothing was actually built from.
    """
    offered = offered_tools(spec, catalog)
    allowed_params = frozenset(
        name for tool in offered for action in tool.actions for name in (action.input_schema.get("properties") or {})
    )
    # Only when the spec *named* its connection: with the whole catalog on
    # offer because no name matched, "which action's schema" is unknown, and
    # demanding a collector for the union of every tool's parameters would
    # refuse honest graphs over a spelling.
    named = bool(connected_tool_ids(spec.data, [(tool.id, tool.name) for tool in catalog]))
    required_from_user = frozenset(
        name
        for tool in offered
        for action in tool.actions
        if named and not action.read_only
        for name in (action.input_schema.get("required") or [])
        if name in _USER_DECIDED_PARAMS
    )
    # The write actions the filter left off the menu, by the words in their
    # names — so an `mcp` node the model calls "mcp_create_draft" on a spec
    # that only ever said *gửi* can be named as the habit it is (rule G).
    offered_names = {tool.id: {action.name for action in tool.actions} for tool in offered}
    unoffered_write_tokens = frozenset(
        token
        for tool in catalog
        if tool.id in offered_names
        for action in tool.actions
        if not action.read_only and action.name not in offered_names[tool.id]
        for token in action.name.lower().replace("-", "_").split("_")
        if token not in _GENERIC_VERBS
    )
    offered_actions = tuple(f"{tool.id}.{action.name}" for tool in offered for action in tool.actions)
    return await write_document(
        build_prompt=lambda: _composition_prompt(spec=spec, model_allowlist=model_allowlist, catalog=offered),
        parse=lambda reply: _graph_from(
            reply,
            spec=spec,
            model_allowlist=model_allowlist,
            allowed_params=allowed_params,
            required_from_user=required_from_user,
            unoffered_write_tokens=unoffered_write_tokens,
            offered_actions=offered_actions,
        ),
        model_factory=model_factory,
        exhausted=_EXHAUSTED,
        failure=lambda message, reason: GraphCompositionError(message, reason=reason),
        purpose=PlatformCallPurpose.COMPOSE_GRAPH,
    )


def _graph_from(
    reply: str,
    *,
    spec: AgentSolutionSpecification,
    model_allowlist: Sequence[str],
    allowed_params: frozenset[str] = frozenset(),
    required_from_user: frozenset[str] = frozenset(),
    unoffered_write_tokens: frozenset[str] = frozenset(),
    offered_actions: tuple[str, ...] = (),
) -> WorkflowJSON:
    """One reply as a validated graph, with what the model does not own put back.

    The order is the one `refinement._graph_from` uses and for the same reasons:
    parse, then overwrite what the model may not decide, then judge. Judging
    first would reject a graph for a blank `tool_id` the model was told not to
    write, and the model would spend its next attempt inventing one.

    `_narrated` runs in between — after the model's nodes exist, before layout
    places them — for the same logic one step further: fixing a graph the model
    forgot to fix is cheaper than asking it to.
    """
    proposed = WorkflowJSON.model_validate(_repaired(json_object(reply)))
    configured = proposed.model_copy(
        update={"nodes": [_node_config(node, spec, model_allowlist) for node in proposed.nodes]}
    )
    graph = laid_out(
        _split_shared_ends(
            _composed_writes(
                _wired_collectors(
                    _relocated_collectors(
                        _collected(
                            _narrated(_deduplicated_reads(configured, spec), model_allowlist), required_from_user
                        )
                    )
                ),
                allowed_params,
            )
        )
    )
    _rejected_for(
        graph,
        spec,
        allowed_params=allowed_params,
        required_from_user=required_from_user,
        unoffered_write_tokens=unoffered_write_tokens,
        offered_actions=offered_actions,
    )
    return graph


def _repaired(document: dict[str, Any]) -> dict[str, Any]:
    """The model's JSON with the one shorthand it keeps writing filled in.

    Measured 2026-08-30: two of two attempts at a 13-node graph ended with
    `{"id": "end"}` — a terminal node written without its `type` — and every
    retry repeated it, because pydantic's *"nodes.13.type Field required"* is
    not a sentence the model reads as "write `\"type\": \"end\"`". A node whose id
    starts with `end` and has no type can only be an `end` node; filling that in
    is the same kind of free repair `_narrated` does, and it keeps the paid
    attempts for problems only the model can fix.
    """
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        return document
    nodes, edges = _nodes_out_of_the_edge_list(nodes, document.get("edges"))
    repaired = []
    for node in nodes:
        if isinstance(node, dict) and not node.get("type") and str(node.get("id", "")).lower().startswith("end"):
            node = {**node, "type": NodeType.END.value, "config": {}}
        if isinstance(node, dict) and "config" not in node:
            node = {**node, "config": {}}
        repaired.append(node)
    document = {**document, "nodes": repaired}
    return document if edges is None else {**document, "edges": edges}


def _nodes_out_of_the_edge_list(
    nodes: list[Any], edges: Any
) -> tuple[list[Any], list[Any] | None]:
    """A node the model wrote into `"edges"` moved back into `"nodes"` — the second shorthand.

    Measured 2026-08-30 on the 13-node reminder-letter graph: three separate
    compositions ended with an `if_else` (and once an `agent_llm`) sitting in the
    edge list, complete with its `config.condition`. Pydantic answers that with
    five errors about `edges.3.source_node_id` and `extra_forbidden`, the model
    reads none of them as *"that was a node"*, and all three attempts burn — the
    graph that reaches the creator is then the nearest catalog template, which is
    how a 13-node workflow becomes a 7-node one nobody asked for.

    An entry with a `type` and no `source_node_id` can only be a node, so moving
    it is the same free repair as filling in a missing `"type": "end"` above.
    What it does **not** do is invent the edges that node needs: a node with an
    unwired port comes back from `_rejected_for` as one sentence naming the port,
    which is a complaint the model has been measured to act on.
    """
    if not isinstance(edges, list):
        return nodes, None
    known = {entry.get("id") for entry in nodes if isinstance(entry, dict)}
    kept: list[Any] = []
    moved: list[Any] = []
    for entry in edges:
        misplaced = isinstance(entry, dict) and entry.get("type") and "source_node_id" not in entry
        if misplaced and entry.get("id") not in known:
            moved.append(entry)
        elif not misplaced:
            kept.append(entry)
    return ([*nodes, *moved], kept) if moved else (nodes, None)


#: What the auto-inserted narrator says. Rule 3 of the composer's own prompt,
#: restated as an instruction rather than a constraint — this text *is* "thêm
#: một bước Agent/LLM ở giữa nó và bước Kết thúc", the validator's own fix,
#: applied by code instead of asked for a fourth time.
_NARRATOR_PROMPT = (
    "Kể lại kết quả của bước công cụ ngay trước bằng lời cho người dùng — đúng những gì kết quả đó có "
    "(mã, tên, liên kết chép nguyên văn). KHÔNG THÊM (không tự chế chi tiết không có trong kết quả), "
    "KHÔNG BỚT (không tự lọc theo điều kiện nào). Kết quả rỗng thì nói rõ là rỗng."
)


def _narrated(graph: WorkflowJSON, model_allowlist: Sequence[str]) -> WorkflowJSON:
    """Splice a narrating `agent_llm` after every `mcp` node the composer left as the last word.

    Measured, not theoretical: across a battery of live compositions this was
    **the** dominant failure — a model that gets seven other rules right and
    then ends a branch straight on an `mcp` node's raw JSON. `raw_payload_as_answer`
    already catches it and already tells the model exactly how to fix it
    (`_rejected_for` feeds that sentence back through `_CORRECTION`); this
    function is that same fix, done once, for free, instead of gambled on a
    fourth attempt.

    Splicing after `_node_config` and before `laid_out` is what makes this a
    pure graph-surgery step with no model call and no position math: a fresh
    node needs no `Position` (it defaults to the origin) because `laid_out`
    recomputes every position on the graph it receives, inserted nodes included.

    Safe by construction, not by policy: `mcp` has exactly one output port
    (`done`), so "insert a node right after M" has exactly one edge to redirect,
    and the narrator is `agent_llm` — a `_SPEAKING_TYPES` member — so the very
    node the walk in `raw_payload_as_answer` would have flagged next simply
    isn't reached; nothing about `graph_problems`'s other rules is touched.
    """
    flagged = {problem.node_id for problem in raw_payload_as_answer(graph) if problem.node_id}
    if not flagged:
        return graph

    existing_ids = {node.id for node in graph.nodes} | {edge.id for edge in graph.edges}

    def _fresh(base: str) -> str:
        candidate, suffix = f"{base}_ke_lai", 2
        while candidate in existing_ids:
            candidate = f"{base}_ke_lai_{suffix}"
            suffix += 1
        existing_ids.add(candidate)
        return candidate

    new_nodes, new_edges = [], []
    for edge in graph.edges:
        if edge.source_node_id not in flagged:
            new_edges.append(edge)
            continue
        narrator_id = _fresh(edge.source_node_id)
        new_nodes.append(
            NodeInstance(
                id=narrator_id,
                type=NodeType.AGENT_LLM,
                config={"prompt": _NARRATOR_PROMPT, "model": default_node_model(model_allowlist)},
            )
        )
        new_edges.append(edge.model_copy(update={"target_node_id": narrator_id}))
        new_edges.append(
            Edge(id=_fresh(edge.id), source_node_id=narrator_id, source_port="done", target_node_id=edge.target_node_id)
        )

    return graph.model_copy(update={"nodes": [*graph.nodes, *new_nodes], "edges": new_edges})


def _composed_writes(graph: WorkflowJSON, allowed_params: frozenset[str]) -> WorkflowJSON:
    """Every `collect` node's `agent_writes` split the one way it can be split — by rule, not by asking.

    The text of a letter or a document (`body`, `content`) is what the graph's
    own `agent_llm` writes; the address or the title (`to`, `title`) is what only
    the user can give. The composer was told this and still left `agent_writes`
    empty on the third attempt of every 13-node graph measured (2026-08-30,
    round 4: the *only* complaint left standing was this one, twice). A rule the
    model cannot get right is a rule the code applies — the same reasoning as
    `_narrated` and `_wired_collectors`.
    """
    composed = sorted(allowed_params & _COMPOSED_PARAMS)
    nodes = []
    for node in graph.nodes:
        if node.type is NodeType.COLLECT:
            written = [str(name) for name in (node.config.get("agent_writes") or [])]
            fixed = [name for name in written if name not in _USER_DECIDED_PARAMS]
            fixed += [name for name in composed if name not in fixed]
            if fixed != written:
                node = node.model_copy(update={"config": {**node.config, "agent_writes": fixed}})
        nodes.append(node)
    return graph.model_copy(update={"nodes": nodes})


def _split_shared_ends(graph: WorkflowJSON) -> WorkflowJSON:
    """Mỗi nhánh một node `end` của riêng nó — cắt bằng code, không xin model.

    Bộ soạn hay dồn cả ba nhánh (đã gửi, bị từ chối, hỏi lại) vào một node `end`
    chung, và trên canvas ba nhánh ấy thành ba đường cắt chéo hội tụ một chỗ —
    người tạo không đọc được nhánh nào đi đâu, trong khi mọi graph đáp án của bộ
    benchmark đều tách. Bảo model tách thì đo được là **hỏng**: một câu thêm vào
    `_COMPOSITION_PROMPT` (30/08) làm nó vừa đẻ ra `end` không cạnh nào vào, vừa
    thổi graph 13 node thành 17 và một lượt còn nhét cả một node vào mảng
    `edges` — mất luôn cả hai lượt đo của mẫu 02.

    Nên đây là phép cắt hình học, một nghĩa duy nhất, làm sau khi model đã viết
    xong: cạnh thứ nhất giữ node `end` gốc, mỗi cạnh còn lại được một bản sao
    mang tên node nguồn của nó. Không đổi đường đi, không đổi cấu hình, không
    thêm nhánh — chỉ thôi bắt bốn nhánh dùng chung một cái tên. Chạy TRƯỚC
    `laid_out` để bản sao được xếp chỗ như mọi node khác.
    """
    incoming: dict[str, list[int]] = {}
    for index, edge in enumerate(graph.edges):
        incoming.setdefault(edge.target_node_id, []).append(index)
    ends = {node.id: node for node in graph.nodes if node.type is NodeType.END}
    shared = {end: rows for end, rows in incoming.items() if end in ends and len(rows) > 1}
    if not shared:
        return graph
    taken = {node.id for node in graph.nodes}
    nodes, edges = list(graph.nodes), list(graph.edges)
    for end_id, rows in shared.items():
        for index in rows[1:]:
            source = edges[index].source_node_id
            clone_id = f"{end_id}_{source}"
            while clone_id in taken:
                clone_id += "_"
            taken.add(clone_id)
            nodes.append(ends[end_id].model_copy(update={"id": clone_id}))
            edges[index] = edges[index].model_copy(update={"target_node_id": clone_id})
    return graph.model_copy(update={"nodes": nodes, "edges": edges})


def _deduplicated_reads(graph: WorkflowJSON, spec: AgentSolutionSpecification) -> WorkflowJSON:
    """Chains of same-type read nodes collapsed to what the spec's sources justify — for free.

    Three `web_search` nodes in a row for one web source (2026-08-30, runs 9, 11,
    12 — one per *aspect* the spec listed: ngành nghề, quy mô, tin gần đây) is
    three paid searches of the same question: each one reads the same subject,
    whether that is the user's message or the extractor drawn in front of the
    first of them (ADR-0068). The composer kept redrawing it through
    all three attempts; collapsing a chain `A → B` of one read type into `A`
    (rewired to `B`'s successor) is graph surgery with one possible meaning, so
    it is done here rather than asked for again. Reads on separate branches are
    left alone — rule F still names them.
    """
    limits = {
        NodeType.KNOWLEDGE_SEARCH: sum(1 for source in spec.data if source.kind is DataSourceKind.UPLOADED_FILE),
        NodeType.WEB_SEARCH: sum(1 for source in spec.data if source.kind is DataSourceKind.WEB),
    }
    changed = True
    while changed:
        changed = False
        nodes = {node.id: node for node in graph.nodes}
        incoming: dict[str, int] = {}
        for edge in graph.edges:
            incoming[edge.target_node_id] = incoming.get(edge.target_node_id, 0) + 1
        for read_type, limit in limits.items():
            if limit == 0 or sum(1 for node in graph.nodes if node.type is read_type) <= limit:
                continue
            for edge in graph.edges:
                first, second = nodes.get(edge.source_node_id), nodes.get(edge.target_node_id)
                if first is None or second is None or first.type is not read_type or second.type is not read_type:
                    continue
                if incoming.get(second.id, 0) != 1 or second.id == graph.start_node_id:
                    continue
                onward = [e for e in graph.edges if e.source_node_id == second.id]
                kept_edges = [e for e in graph.edges if e.id != edge.id and e.source_node_id != second.id]
                kept_edges.extend(edge.model_copy(update={"target_node_id": e.target_node_id}) for e in onward[:1])
                graph = graph.model_copy(
                    update={"nodes": [node for node in graph.nodes if node.id != second.id], "edges": kept_edges}
                )
                changed = True
                break
            if changed:
                break
    return graph


def _collected(graph: WorkflowJSON, required_from_user: frozenset[str]) -> WorkflowJSON:
    """A `collect` in front of every `mcp` node whose action needs something only the user can give.

    Rule B as a repair rather than a refusal. The biên-bản graph on 2026-08-30
    (run 14) was redrawn three times without a collector for `title`, then thrown
    away for a two-node template — over a node whose whole shape is fixed by
    ADR-0052: `action_node` → the mcp node, `complete` → the mcp node,
    `incomplete` → an `end`. Inserted directly in front of the mcp node, taking
    over its incoming edges; the prompt still asks for it earlier on the path,
    and this is what happens when the model does not listen. Nothing to do when
    the spec named no connection (`required_from_user` is empty then).
    """
    if not required_from_user:
        return graph
    targeted = {str(node.config.get("action_node") or "") for node in graph.nodes if node.type is NodeType.COLLECT}
    exposed = [node for node in graph.nodes if node.type is NodeType.MCP and node.id not in targeted]
    if not exposed:
        return graph
    existing_ids = {node.id for node in graph.nodes} | {edge.id for edge in graph.edges}

    def _fresh(base: str) -> str:
        candidate, suffix = base, 2
        while candidate in existing_ids:
            candidate = f"{base}_{suffix}"
            suffix += 1
        existing_ids.add(candidate)
        return candidate

    end = next((node for node in graph.nodes if node.type is NodeType.END), None)
    new_nodes: list[NodeInstance] = []
    if end is None:
        end = NodeInstance(id=_fresh("end_hoi"), type=NodeType.END, config={})
        new_nodes.append(end)
    edges = list(graph.edges)
    start = graph.start_node_id
    for node in exposed:
        collector_id = _fresh(f"thu_thap_{node.id}")
        new_nodes.append(NodeInstance(id=collector_id, type=NodeType.COLLECT, config={"action_node": node.id, "agent_writes": []}))
        edges = [
            edge.model_copy(update={"target_node_id": collector_id}) if edge.target_node_id == node.id else edge
            for edge in edges
        ]
        edges.append(Edge(id=_fresh(f"{collector_id}_complete"), source_node_id=collector_id, source_port="complete", target_node_id=node.id))
        edges.append(Edge(id=_fresh(f"{collector_id}_incomplete"), source_node_id=collector_id, source_port="incomplete", target_node_id=end.id))
        if start == node.id:
            start = collector_id
    return graph.model_copy(update={"start_node_id": start, "nodes": [*graph.nodes, *new_nodes], "edges": edges})


def _relocated_collectors(graph: WorkflowJSON) -> WorkflowJSON:
    """On a graph with no approval, every `collect` moved to sit directly in front of its `mcp` node.

    The prompt's placement rule has an exception (collect first when a
    `human_approval` follows, so the letter is drafted only once the address is
    known) and the composer sometimes applies the exception where it does not
    hold: a biên-bản graph with no approval drew `collect → knowledge_search →
    while → … → mcp` (2026-08-30, run 36), asking for the title before a single
    line was written. Where nothing is approved, "directly before the action" is
    the only placement with a reason behind it (ADR-0052), so it is applied by
    code: the collector is unhooked from where it sat (its predecessors rewired
    to its `complete` successor) and re-inserted in front of the action.
    """
    if any(node.type is NodeType.HUMAN_APPROVAL for node in graph.nodes):
        return graph
    nodes = {node.id: node for node in graph.nodes}
    for collector in [node for node in graph.nodes if node.type is NodeType.COLLECT]:
        target_id = str(collector.config.get("action_node") or "")
        target = nodes.get(target_id)
        if target is None or target.type is not NodeType.MCP:
            continue
        complete = next((e for e in graph.edges if e.source_node_id == collector.id and e.source_port == "complete"), None)
        if complete is None or complete.target_node_id == target_id:
            continue  # already directly in front of its action
        successor = complete.target_node_id
        edges = []
        for edge in graph.edges:
            if edge.source_node_id == collector.id:
                continue  # both of the collector's own edges go
            if edge.target_node_id == collector.id:
                edges.append(edge.model_copy(update={"target_node_id": successor}))
            elif edge.target_node_id == target_id:
                edges.append(edge.model_copy(update={"target_node_id": collector.id}))
            else:
                edges.append(edge)
        start = successor if graph.start_node_id == collector.id else graph.start_node_id
        graph = graph.model_copy(update={"start_node_id": start, "edges": edges})
        # `complete` → action; `incomplete` is re-wired by `_wired_collectors` next.
        edge_ids = {edge.id for edge in graph.edges}
        edge_id = f"{collector.id}_complete"
        while edge_id in edge_ids:
            edge_id = f"{edge_id}_2"
        graph = graph.model_copy(
            update={
                "edges": [
                    *graph.edges,
                    Edge(id=edge_id, source_node_id=collector.id, source_port="complete", target_node_id=target_id),
                ]
            }
        )
    return graph


def _wired_collectors(graph: WorkflowJSON) -> WorkflowJSON:
    """Every `collect` node's `incomplete` port wired to an `end` — the one edge the composer keeps forgetting.

    `_narrated`'s reasoning, one node type over: the third attempt at the 13-node
    reminder-letter graph on 2026-08-30 failed on *"Cổng 'incomplete' của node
    'collect_input' chưa nối đi đâu"* and nothing else worth a fourth call. The
    port can only ever mean one thing — the question the collector just asked
    is the turn's answer, so the run ends (ADR-0052) — and an edge to an `end`
    node is that meaning written down. Reuses an existing `end` when there is
    one, adds `end_hoi` otherwise.
    """
    wired = {edge.source_node_id for edge in graph.edges if edge.source_port == "incomplete"}
    missing = [node for node in graph.nodes if node.type is NodeType.COLLECT and node.id not in wired]
    if not missing:
        return graph
    existing_ids = {node.id for node in graph.nodes} | {edge.id for edge in graph.edges}
    end = next((node for node in graph.nodes if node.type is NodeType.END), None)
    new_nodes: list[NodeInstance] = []
    if end is None:
        end_id = "end_hoi"
        while end_id in existing_ids:
            end_id = f"{end_id}_2"
        end = NodeInstance(id=end_id, type=NodeType.END, config={})
        new_nodes.append(end)
    new_edges: list[Edge] = []
    for node in missing:
        edge_id = f"{node.id}_incomplete"
        while edge_id in existing_ids:
            edge_id = f"{edge_id}_2"
        existing_ids.add(edge_id)
        new_edges.append(Edge(id=edge_id, source_node_id=node.id, source_port="incomplete", target_node_id=end.id))
    return graph.model_copy(update={"nodes": [*graph.nodes, *new_nodes], "edges": [*graph.edges, *new_edges]})


def _rejected_for(
    graph: WorkflowJSON,
    spec: AgentSolutionSpecification,
    *,
    allowed_params: frozenset[str] = frozenset(),
    required_from_user: frozenset[str] = frozenset(),
    unoffered_write_tokens: frozenset[str] = frozenset(),
    offered_actions: tuple[str, ...] = (),
) -> None:
    """Raise `DocumentRejectedError` naming everything wrong with `graph`, or return.

    Every complaint at once rather than the first, which matters more here than
    it does on a screen: each attempt is a paid call, and a model told one
    problem per round would spend all three of them learning what one round could
    have taught it.

    `structural_problems` is the graph validator, asked without its blank-field
    report (see its docstring) — a `tool_id` this module deliberately left
    empty is not a fault of the model's drawing.

    What is deliberately **not** here any more: a complaint that the spec
    "has an irreversible action" and the graph has no approval. That verdict
    is `src.workflow.effects`' word-list over the creator's prose — a
    heuristic, by its own account — and it once read *"người gửi"* (the
    sender column of a mailbox listing) as *gửi*, told the composer a
    read-only search needed an approval, refused the three honest graphs it
    drew, and handed the creator a document-QA template with an empty
    `knowledge_search` that could not be confirmed. The heuristic still shapes
    the *advice* (`_APPROVAL_REQUIRED` in the prompt); the *refusal* belongs to
    confirmation, which reads the risk of the action actually bound
    (`ensure_confirmable`) and blocks on a fact rather than on a word.
    """
    complaints = [problem.message for problem in structural_problems(graph)]
    # `_raw_payload_as_answer` is a *warning* for a creator-drawn graph (ending
    # on an action is a real shape). A composed graph is the chat's answer to a
    # person, so here it is a refusal: the baseline run produced two graphs
    # whose last word was `{"draft_id": …}` / `{"sent": true}`, and every
    # judged case about "confirm what was sent" failed on the JSON.
    complaints.extend(problem.message for problem in raw_payload_as_answer(graph))
    if len(graph.nodes) > MAX_NODES:
        complaints.append(f"Workflow có {len(graph.nodes)} node, vượt trần {MAX_NODES} node của hệ thống")
    complaints.extend(
        _shape_complaints(graph, spec, allowed_params, required_from_user, unoffered_write_tokens, offered_actions)
    )
    if complaints:
        raise DocumentRejectedError("; ".join(complaints))


#: A `condition` that reads like code rather than like the creator's business.
#: Every alternative here was drawn by the composer on 2026-08-30
#: (`knowledge_search_policy.found == true`, `assess_result.is_ready_to_send ==
#: true`, *"Kết quả node check_complete là 'Đủ'"*), and every one of them is a
#: sentence the run-time router (`_condition_holds`) cannot decide, because it
#: is shown the question, the last answer and the evidence — never node ids.
_CODE_CONDITION = re.compile(r"==|!=|\.found\b|\.done\b|\btrue\b|\bfalse\b|\bnull\b|kết quả node|\bnode\b", re.IGNORECASE)


def _shape_complaints(
    graph: WorkflowJSON,
    spec: AgentSolutionSpecification,
    allowed_params: frozenset[str],
    required_from_user: frozenset[str] = frozenset(),
    unoffered_write_tokens: frozenset[str] = frozenset(),
    offered_actions: tuple[str, ...] = (),
) -> list[str]:
    """Shapes the validator accepts and the composer still gets wrong — each one measured, none hypothetical.

    `structural_problems` answers "could the runtime execute this"; these answer
    "does this graph do what the spec asked". Both are fed back through the same
    correction loop, because a graph that runs and reads a policy shelf through
    an invented `mcp` node is not a draft a creator can use. All five were the
    dominant failures of the 2026-08-30 measurement (`benchmark/pitch_kinh_doanh`):

    - `mcp`/`collect` on a spec with no connected account (8/16 graphs);
    - conditions written as code (4/16);
    - `human_approval` guarding nothing, or `rejected` wired straight to `end`;
    - `agent_writes` naming a parameter the action does not have (`body` on
      `create_doc`);
    - a read node with no incoming edge, which confirms and never runs.
    """
    complaints: list[str] = []
    nodes = {node.id: node for node in graph.nodes}
    if not _has_connection(spec):
        offenders = [node.id for node in graph.nodes if node.type in (NodeType.MCP, NodeType.COLLECT)]
        if offenders:
            complaints.append(
                "Bài toán không có tài khoản kết nối nào nên không được có node mcp hay collect; hãy bỏ "
                f"{', '.join(offenders)} — câu người dùng gõ là đầu vào, tài liệu đã tải lên đọc bằng knowledge_search"
            )
    for node in graph.nodes:
        if node.type in (NodeType.IF_ELSE, NodeType.WHILE):
            condition = str(node.config.get("condition") or "")
            if _CODE_CONDITION.search(condition):
                complaints.append(
                    f"Node {node.id!r} có condition viết kiểu mã hoặc nhắc tên node ({condition[:80]!r}); "
                    "viết lại thành một câu nghiệp vụ về điều node trước nó vừa kết luận"
                )
    has_mcp = any(node.type is NodeType.MCP for node in graph.nodes)
    if not has_mcp and any(node.type is NodeType.HUMAN_APPROVAL for node in graph.nodes):
        complaints.append("Graph có human_approval nhưng không có node mcp nào để duyệt; hãy bỏ node duyệt")
    for edge in graph.edges:
        source, target = nodes.get(edge.source_node_id), nodes.get(edge.target_node_id)
        if (
            source is not None
            and target is not None
            and source.type is NodeType.HUMAN_APPROVAL
            and edge.source_port == "rejected"
            and target.type is NodeType.END
        ):
            complaints.append(
                f"Cổng rejected của {source.id!r} nối thẳng vào end; phải đi qua một node agent_llm báo rõ "
                "việc chưa được làm và vì sao"
            )
    if allowed_params:
        for node in graph.nodes:
            if node.type is not NodeType.COLLECT:
                continue
            writes = [str(name) for name in (node.config.get("agent_writes") or [])]
            wrong = [name for name in writes if name not in allowed_params]
            if wrong:
                complaints.append(
                    f"Node {node.id!r} ghi agent_writes {wrong} không phải tham số của action nào trong bảng; "
                    f"tên đúng nằm trong {sorted(allowed_params)}"
                )
            # `to`/`title` in agent_writes and a missing `body`/`content` are
            # not complaints any more: `_composed_writes` fixes both for free
            # before this runs, for the reason its docstring gives.
    spec_text = _spec_text(spec)
    threshold = _THRESHOLD.search(spec_text)
    if threshold and not any(node.type is NodeType.IF_ELSE for node in graph.nodes):
        complaints.append(
            f"Bài toán nêu ngưỡng {threshold.group(0)!r} nhưng graph không có node if_else nào; mỗi ngưỡng là một "
            "if_else với condition nêu đúng ngưỡng đó, hai cổng dẫn tới hai bước khác nhau"
        )
    # K — a threshold in a condition is the spec's threshold, not one the model remembers from somewhere.
    #
    # This used to run only `if spec_numbers`, which skipped the case it is most needed for. Measured
    # 2026-08-31 on the reminder-letter spec, runs 58 and 59: the spec carried no quantity at all in any
    # of the five fields the composer reads, and the model still drew `if_else` conditions "Số ngày quá
    # hạn nhỏ hơn hoặc bằng 30 ngày" and "Khoản nợ đã quá hạn từ 30 ngày trở lên". Thirty is not in the
    # spec, not in the description, and not the tier the company's policy uses — it is a number the model
    # remembered from somewhere. A spec that states no threshold makes *every* quantity in a condition a
    # foreign one, so that is the strongest case for the rule, not an exemption from it.
    #
    # It stays a complaint rather than a repair because the repair does not exist: nothing in the spec says
    # what the right number would be. Three attempts and then the template fallback is loud; a graph that
    # silently sorts customers by a 30-day rule nobody wrote is not.
    spec_numbers = {match.group(0) for match in _QUANTITY.finditer(spec_text)}
    spec_digits = {re.sub(r"\D", "", quantity) for quantity in spec_numbers}
    for node in graph.nodes:
        if node.type not in (NodeType.IF_ELSE, NodeType.WHILE):
            continue
        condition = str(node.config.get("condition") or "").lower()
        foreign = sorted(
            quantity
            for quantity in {match.group(0) for match in _QUANTITY.finditer(condition)}
            if re.sub(r"\D", "", quantity) not in spec_digits
        )
        if not foreign:
            continue
        complaints.append(
            (
                f"Node {node.id!r} dùng ngưỡng {foreign} không có trong bài toán; ngưỡng bài toán nêu là "
                f"{sorted(spec_numbers)} — condition phải dùng đúng con số đó"
            )
            if spec_numbers
            else (
                f"Node {node.id!r} dùng ngưỡng {foreign} mà bài toán KHÔNG nêu một con số nào; không được tự "
                "đặt ra ngưỡng — viết condition bằng lời nghiệp vụ theo đúng thứ bài toán nói, hoặc bỏ node này"
            )
        )
    incoming = {edge.target_node_id for edge in graph.edges}
    for node in graph.nodes:
        if (
            node.type in (NodeType.KNOWLEDGE_SEARCH, NodeType.WEB_SEARCH)
            and node.id != graph.start_node_id
            and node.id not in incoming
        ):
            complaints.append(f"Node {node.id!r} không có cạnh nào đi vào nên không bao giờ chạy; nối nó vào đường đi")
    # B — an action whose required parameter only the user can give needs a collector in front of it.
    if required_from_user:
        targeted = {str(node.config.get("action_node") or "") for node in graph.nodes if node.type is NodeType.COLLECT}
        for node in graph.nodes:
            if node.type is NodeType.MCP and node.id not in targeted:
                complaints.append(
                    f"Node mcp {node.id!r} cần một node collect đứng trước (action_node trỏ tới nó) để hỏi người dùng "
                    f"{sorted(required_from_user)} khi họ chưa đưa; cổng incomplete → end"
                )
    # (A collector wired straight into the approval used to be refused here. It is
    # not any more: `runtime._latest_spoken_answer` now shows the reviewer the
    # last thing an `agent_llm` wrote, so the placement stopped being a fault.)
    # G — an mcp node whose own name says it calls a write action the spec never asked for.
    if unoffered_write_tokens:
        for node in graph.nodes:
            if node.type is NodeType.MCP:
                hit = sorted(set(node.id.lower().replace("-", "_").split("_")) & unoffered_write_tokens)
                if hit:
                    complaints.append(
                        f"Node mcp {node.id!r} nhắm tới action {hit} mà bài toán không yêu cầu; chỉ dùng action trong "
                        "bảng, mỗi hành động nghiệp vụ đúng một node mcp — bỏ node này"
                    )
    # I — one mcp node per offered action: the spec asked for one send, the graph may not contain three.
    if offered_actions:
        mcp_nodes = [node.id for node in graph.nodes if node.type is NodeType.MCP]
        if len(mcp_nodes) > len(offered_actions):
            complaints.append(
                f"Graph có {len(mcp_nodes)} node mcp ({', '.join(mcp_nodes)}) nhưng bài toán chỉ dùng "
                f"{len(offered_actions)} action ({', '.join(offered_actions)}); mỗi action đúng một node mcp — bỏ node "
                "thừa; tài liệu đã tải lên đọc bằng knowledge_search, không phải mcp"
            )
    # J — a graph that only reads and answers has exactly one answering node: a chain of three agent_llm
    # nodes after the reads (summarise web, summarise policy, compose — 2026-08-30 runs 13–14) all see the
    # same findings and pay three times for one answer.
    types_present = {node.type for node in graph.nodes}
    only_reads = not types_present & {
        NodeType.MCP,
        NodeType.COLLECT,
        NodeType.IF_ELSE,
        NodeType.WHILE,
        NodeType.HUMAN_APPROVAL,
    }
    # An extractor is not an answering node: it writes the search terms a `web_search` node behind it
    # sends (ADR-0068) and nobody reads what it wrote. Counting it would make rule J refuse the one shape
    # that lets a web search be about a company name buried in a sentence.
    extractors = {
        edge.source_node_id
        for edge in graph.edges
        if (source := nodes.get(edge.source_node_id)) is not None
        and (target := nodes.get(edge.target_node_id)) is not None
        and source.type is NodeType.AGENT_LLM
        and target.type is NodeType.WEB_SEARCH
    }
    answering = [node.id for node in graph.nodes if node.type is NodeType.AGENT_LLM and node.id not in extractors]
    if only_reads and len(answering) > 1:
        complaints.append(
            f"Graph chỉ đọc rồi trả lời nhưng có {len(answering)} node agent_llm ({', '.join(answering)}); các node "
            "đọc nối tiếp nhau rồi MỘT agent_llm duy nhất trả lời từ mọi bằng chứng — gộp thành một node"
        )
    # H — a `knowledge_search` node fed by an agent_llm never uses what that node wrote: it searches the
    # shelf with the user's own words, which is what a retriever scores passages against. `web_search` is
    # the exception since ADR-0068 — an agent_llm in front of one is its extractor, and the runtime reads it.
    for edge in graph.edges:
        source, target = nodes.get(edge.source_node_id), nodes.get(edge.target_node_id)
        if (
            source is not None
            and target is not None
            and source.type is NodeType.AGENT_LLM
            and target.type is NodeType.KNOWLEDGE_SEARCH
        ):
            complaints.append(
                f"Node agent_llm {source.id!r} nối vào node knowledge_search {target.id!r}: node đó tìm trong tài "
                "liệu theo câu người dùng hỏi, không đọc thứ agent_llm viết — nối thẳng node đọc vào đường chính"
            )
    # C — every kind of source the spec names has its node, and no node reads a source the spec lacks.
    kinds = {source.kind for source in spec.data}
    types = {node.type for node in graph.nodes}
    # F — one read node per source: three web_search nodes for one web source is three paid searches for one answer.
    web_sources = sum(1 for source in spec.data if source.kind is DataSourceKind.WEB)
    web_nodes = sum(1 for node in graph.nodes if node.type is NodeType.WEB_SEARCH)
    if web_sources and web_nodes > web_sources:
        complaints.append(f"Graph có {web_nodes} node web_search cho {web_sources} nguồn web; mỗi nguồn web đúng một node")
    file_sources = sum(1 for source in spec.data if source.kind is DataSourceKind.UPLOADED_FILE)
    file_nodes = sum(1 for node in graph.nodes if node.type is NodeType.KNOWLEDGE_SEARCH)
    if file_sources and file_nodes > file_sources:
        complaints.append(
            f"Graph có {file_nodes} node knowledge_search cho {file_sources} tài liệu; một node tìm được trên cả kệ"
        )
    if DataSourceKind.UPLOADED_FILE in kinds and NodeType.KNOWLEDGE_SEARCH not in types:
        complaints.append("Bài toán có tài liệu đã tải lên nhưng graph không có node knowledge_search nào để đọc nó")
    if DataSourceKind.WEB in kinds and NodeType.WEB_SEARCH not in types:
        complaints.append("Bài toán có nguồn tìm trên web nhưng graph không có node web_search nào")
    if DataSourceKind.WEB not in kinds and NodeType.WEB_SEARCH in types:
        complaints.append("Bài toán không có nguồn web nào; bỏ node web_search")
    if DataSourceKind.UPLOADED_FILE not in kinds and NodeType.KNOWLEDGE_SEARCH in types:
        complaints.append("Bài toán không có tài liệu tải lên nào; bỏ node knowledge_search")
    if DataSourceKind.CONNECTED_ACCOUNT in kinds and offered_actions and NodeType.MCP not in types:
        complaints.append(
            "Bài toán có tài khoản kết nối và một action phải gọi "
            f"({', '.join(offered_actions)}) nhưng graph không có node mcp nào — hành động ra ngoài là một node mcp"
        )
    # D — the body of a while loop is for the agent's own rework; nothing in it may act on the world or stop the run.
    leaving: dict[str, list[tuple[str, str]]] = {}
    for edge in graph.edges:
        leaving.setdefault(edge.source_node_id, []).append((edge.source_port, edge.target_node_id))
    for loop in graph.nodes:
        if loop.type is not NodeType.WHILE:
            continue
        body = _loop_body(loop.id, leaving)
        offenders = [
            node.id
            for node in graph.nodes
            if node.id in body and node.type in (NodeType.MCP, NodeType.COLLECT, NodeType.HUMAN_APPROVAL)
        ]
        if offenders:
            complaints.append(
                f"Thân vòng lặp của {loop.id!r} chứa {offenders}: mcp, collect và human_approval phải đứng SAU cổng "
                "exit của while, không nằm trong chu trình"
            )
    return complaints


def _loop_body(loop_id: str, leaving: dict[str, list[tuple[str, str]]]) -> set[str]:
    """The nodes on some path from `loop_id`'s `loop` port back to `loop_id` — its body."""

    def reaches_loop(start: str) -> set[str]:
        seen: set[str] = set()
        pending = [start]
        while pending:
            current = pending.pop()
            if current in seen or current == loop_id:
                continue
            seen.add(current)
            pending.extend(target for _, target in leaving.get(current, []))
        return seen

    body: set[str] = set()
    for port, target in leaving.get(loop_id, []):
        if port != "loop":
            continue
        forward = reaches_loop(target)
        for node_id in forward:
            # a node is in the body only if the loop node is reachable from it again
            if _reaches(node_id, loop_id, leaving):
                body.add(node_id)
    return body


def _reaches(start: str, goal: str, leaving: dict[str, list[tuple[str, str]]]) -> bool:
    seen: set[str] = set()
    pending = [start]
    while pending:
        current = pending.pop()
        if current == goal:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(target for _, target in leaving.get(current, []))
    return False


def _has_connection(spec: AgentSolutionSpecification) -> bool:
    """Whether the spec names any connected account — the only thing that licenses an `mcp` node."""
    return any(source.kind is DataSourceKind.CONNECTED_ACCOUNT for source in spec.data)


def offered_tools(spec: AgentSolutionSpecification, catalog: tuple[CatalogTool, ...]) -> tuple[CatalogTool, ...]:
    """The connections the composer may draw on: those the spec's own data names.

    The creator's whole catalog used to go in, and the measured cost was a
    composer that "read" a policy shelf through Gmail. A spec that names no
    account offers nothing; one that names an account the matcher cannot place
    (`connected_tool_ids`) falls back to the whole catalog, because refusing a
    real connection over a spelling is the worse error.
    """
    if not _has_connection(spec):
        return ()
    mentioned = set(connected_tool_ids(spec.data, [(tool.id, tool.name) for tool in catalog]))
    offered = tuple(tool for tool in catalog if tool.id in mentioned) or catalog
    text = _spec_text(spec)
    return tuple(_offered_actions(tool, text) for tool in offered)


#: Parameters a graph's own `agent_llm` writes — never something to ask the user
#: to type. `body` and `content` are the built-in actions' names for "the text";
#: a registered MCP server naming its prose differently is simply not covered,
#: which costs nothing (the rule then does not fire).
_COMPOSED_PARAMS: frozenset[str] = frozenset({"body", "content"})

#: Parameters only the user can decide, so they may never sit in `agent_writes`.
_USER_DECIDED_PARAMS: frozenset[str] = frozenset({"to", "title", "file_id", "message_id"})

#: A numeric threshold stated in the spec's own words — "từ 60 ngày trở lên",
#: "trên 10.000.000 đồng". Measured 2026-08-30: a spec that spelled its tiers
#: out this plainly was drawn four times without an `if_else`, each time as
#: one prompt saying "soạn đúng giọng của bậc đã xác định".
_THRESHOLD = re.compile(
    r"\b(từ|trên|dưới|quá|hơn|ít nhất|tối đa|đủ)\s+\d[\d.,]*\s*(ngày|giờ|tuần|tháng|%|phần trăm|đồng|triệu|tỷ|lần)",
    re.IGNORECASE,
)

#: A number with its unit — what rule K compares between a condition and the
#: spec. Runs 28–29 of the 2026-08-30 measurement: the spec said *60 ngày*, the
#: composer wrote *"quá hạn từ 30 ngày trở lên"*, and nothing objected.
_QUANTITY = re.compile(r"\d[\d.,]*\s*(ngày|giờ|tuần|tháng|%|phần trăm|đồng|triệu|tỷ|lần)", re.IGNORECASE)

#: What a write action is *about*, keyed by the non-generic token of its name,
#: with the words a creator uses for it (diacritics stripped). A write action
#: whose subject the spec never mentions is not offered: the composer shown
#: `create_draft` beside `send_email` drew a "prepare draft" step in front of
#: every letter it was asked to send (2026-08-30, 4 of 4 graphs).
#: Vietnamese entries keep their diacritics and are matched against the raw
#: text; ASCII entries are matched against the diacritic-stripped text. Not a
#: style choice: stripped, *nháp* (draft), *nhập* (enter) and *nhắc* (remind)
#: are all "nhap"/"nhac", and "do nhân viên nhập" offered `create_draft` to a
#: reminder-letter spec (2026-08-30, run 13).
_ACTION_WORDS: dict[str, tuple[str, ...]] = {
    "draft": ("nháp", "draft"),
    "email": ("thư", "email", "mail", "gửi"),
    "doc": ("tài liệu", "doc", "văn bản", "biên bản", "báo cáo", "lưu"),
    "file": ("tệp", "file", "tài liệu"),
    "event": ("sự kiện", "lịch", "event"),
    "message": ("tin nhắn", "message", "thư"),
}
_GENERIC_VERBS: frozenset[str] = frozenset({"create", "send", "get", "search", "read", "fetch", "list", "update", "delete", "add", "new"})

#: The built-in **read** actions, keyed by (connection, action), with the words a
#: creator uses when the agent is meant to read that account. Offered only when
#: one appears; a read that is merely *available* is a read the composer bends
#: to some other purpose.
_READ_ACTION_WORDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("gmail", "search_emails"): ("tìm thư", "tìm email", "tìm mail", "tra thư", "tra email", "lọc thư", "tìm trong hộp thư"),
    ("gmail", "get_recent_emails"): ("thư gần", "thư mới", "hộp thư đến", "inbox", "email gần", "mail gần", "thư vừa"),
    ("gmail", "read_email"): ("đọc thư", "đọc email", "đọc mail", "đọc nội dung thư", "đọc nội dung email"),
    ("gmail", "get_profile"): ("tài khoản nào", "địa chỉ của mình", "profile", "hộp thư của ai"),
    ("google_drive", "search"): ("tìm tệp", "tìm file", "tìm tài liệu", "tìm trong drive", "tìm trên drive", "tra trên drive"),
    ("google_drive", "recent_documents"): ("tệp gần", "file gần", "tài liệu gần", "sửa gần", "mới sửa", "gần đây"),
    ("google_drive", "fetch"): ("đọc tệp", "đọc file", "đọc tài liệu", "nội dung tệp", "nội dung file", "mở tài liệu", "đọc từ drive"),
    ("google_drive", "get_profile"): ("tài khoản nào", "drive của ai", "profile"),
}


def _mentions(word: str, raw: str, stripped: str) -> bool:
    """Whether the creator's text contains `word` — with diacritics when the word has them."""
    return word in raw if not word.isascii() else word in stripped


def _spec_text(spec: AgentSolutionSpecification) -> str:
    """Everything the creator said, as one lower-cased string for the word rules."""
    return " ".join(
        [
            spec.goal,
            *spec.actions,
            *spec.expected_outcome,
            *(criterion.text for criterion in spec.success_criteria),
            *spec.prohibitions,
        ]
    ).lower()


def _offered_actions(tool: CatalogTool, spec_text: str) -> CatalogTool:
    """`tool` with the write actions the spec never talks about left out.

    Read-only actions are always offered — reading costs nothing and the spec
    rarely names a lookup. A write action is offered when a word for its subject
    (`_ACTION_WORDS`) appears in the creator's own text, or when its name has no
    entry in the table at all (a registered server's verbs are its own). If that
    would leave a connection with no write action, everything is offered: a
    filter that empties the menu has guessed wrong.
    """
    raw = spec_text.lower()
    stripped = without_diacritics(spec_text)
    kept = []
    for action in tool.actions:
        if action.read_only:
            # A read the creator never asked for is a read the composer will
            # misuse: shown `search_emails` beside `send_email`, it drew an
            # `mcp_read_policy` to "read" a policy shelf (2026-08-30, run 12).
            # Built-in reads are offered only when the spec talks about
            # reading that account; a registered server's reads (no entry)
            # are offered as they are.
            words = _READ_ACTION_WORDS.get((tool.id, action.name))
            if words is None or any(_mentions(word, raw, stripped) for word in words):
                kept.append(action)
            continue
        subjects = [token for token in action.name.lower().replace("-", "_").split("_") if token not in _GENERIC_VERBS]
        words = [word for token in subjects for word in _ACTION_WORDS.get(token, ())]
        if not words or any(_mentions(word, raw, stripped) for word in words):
            kept.append(action)
    if not any(not action.read_only for action in kept) and any(not action.read_only for action in tool.actions):
        return tool
    return replace(tool, actions=tuple(kept))


def _node_config(node: NodeInstance, spec: AgentSolutionSpecification, model_allowlist: Sequence[str]) -> NodeInstance:
    """One proposed node, cut down to the fields its type declares, resources restored.

    Built key by key rather than patched, which is `refinement._config_of`'s
    reasoning and closes the same gap: a model that writes a `tool_id` onto a
    `human_approval` node is putting a resource reference somewhere the declared
    resource fields do not reach, and a patch would leave it there. Keeping only
    what the type declares means an invented value has nowhere to land whatever
    node it was addressed to.

    `model` is the one field the model *does* get to choose here and does not on
    the template path, because a composer that decides a node is the hard one is
    exactly the thing that should say so. It is checked against the allowlist
    rather than trusted — an unlisted or blank answer becomes the platform
    default, silently, because being wrong about which model a step wants is not
    worth another paid attempt.
    """
    declared = config_model_of(node.type).model_fields
    # `mode="json"`, because a node's `config` is a stored JSON document and
    # nothing else in it is a Python object. Plain `model_dump()` hands back an
    # enum *member* for an enum-typed field, and `McpConfig.require_approval` is
    # the first of those — a `StrEnum` compares and serialises like its value, so
    # it would sit in a saved workflow unnoticed until something reached for a
    # `type()` rather than an `==`.
    blanks = config_model_of(node.type)().model_dump(mode="json")
    resources = resource_config(node, spec, model_allowlist=model_allowlist)
    written = {
        field: node.config.get(field, blanks[field]) for field in declared if not is_resource_field(node.type, field)
    }
    config = {**blanks, **written, **resources}
    if node.type is NodeType.AGENT_LLM:
        config["model"] = _assigned_model(node.config.get("model"), model_allowlist)
    return node.model_copy(update={"config": config})


def _assigned_model(written: object, model_allowlist: Sequence[str]) -> str:
    """The model this node runs on: the one it asked for, undominated, else the default.

    `undominated` is the second check and it is not a second opinion about the
    composer's judgement — it only ever swaps a model for one that is cheaper on
    both rates *and* not worse at the job. Choosing a tier is what the composer
    is for; choosing which SKU realises that tier is a price list, and a model
    reaching for the name it has read most often is not reading one. Measured
    three runs in a row: the prompt asked for cheapest-first and got
    `gpt-4o`, $2.50/$10.00, where the same tier was on offer at $2.00/$8.00.
    """
    if isinstance(written, str) and written.strip() in model_allowlist and written.strip() in ASSIGNABLE_MODELS:
        return undominated(written.strip(), model_allowlist)
    return default_node_model(model_allowlist)


def _composition_prompt(
    *, spec: AgentSolutionSpecification, model_allowlist: Sequence[str], catalog: tuple[CatalogTool, ...] = ()
) -> str:
    """The whole brief: the problem, the palette, the models, the creator's actions, and the rules.

    The actions are the creator's catalog with each action's parameters — what
    the composer never used to see. It still does not bind `tool_id`/`action`
    (those are the creator's), but the "đã đủ thông tin?" condition it writes
    before an action has to name the action's parameters and nothing else, and
    a model that has not seen them writes the condition from the spec's prose
    instead — which is how "email người chủ trì" became a required field of a
    `send_email` that has no such parameter.
    """
    return _COMPOSITION_PROMPT.format(
        problem=problem_block(spec),
        catalogue=_catalogue_block(),
        models=_models_block(model_allowlist),
        tools=_tools_block(catalog),
        max_nodes=MAX_NODES,
        approval_rule=_APPROVAL_REQUIRED if has_high_risk_action(spec.actions) else _APPROVAL_ADVISED,
        resource_fields=", ".join(f'"{field}"' for field in sorted(_all_resource_fields())),
        skeleton=_SKELETON,
    )


def _tools_block(catalog: tuple[CatalogTool, ...]) -> str:
    """The creator's actions, one line each with parameters — `src.metaagent.catalog`'s rendering."""
    if not catalog:
        return (
            "(bài toán này không dùng tài khoản kết nối nào — KHÔNG vẽ node mcp, KHÔNG vẽ node collect; "
            "chỉ dựng những gì không cần action bên ngoài)"
        )
    return "\n".join(catalog_lines(catalog))


def _catalogue_block() -> str:
    """Every node type with its ports and its config fields, read off `NODE_REGISTRY`.

    Generated rather than written out, for the reason `refinement._catalogue_block`
    gives: a prompt carrying its own copy of the eight types is a prompt that can
    fall behind ADR-0021 §2 without anything going red.

    Says more than that one does, though, because this model is choosing types
    rather than editing a graph that already has them. Each field is marked with
    who fills it — the model, or the platform — so the rule about resource fields
    arrives attached to the fields it is about rather than only as a sentence
    further down.
    """
    lines = []
    for node_type in NODE_REGISTRY:
        ports = ", ".join(outputs_of(node_type)) or "không có cổng nào, đây là node kết thúc"
        fields = ", ".join(_field_notes(node_type)) or "không có gì để cấu hình"
        lines.append(f'- "{node_type.value}": cổng ra [{ports}]; config: {fields}')
    return "\n".join(lines)


def _field_notes(node_type: NodeType) -> list[str]:
    """Each config field of `node_type`, said with its **shape** and who fills it.

    The shape was the missing half, and it cost real attempts. `agent_writes` was
    described only as *"Tham số do một node sau soạn ra"* — a sentence a model can
    agree with, which is exactly what one did: it wrote `"agent_writes": true`,
    meaning *yes, a later node writes it*, and the graph was refused three times
    for a field whose type had never been stated. The description says what the
    field means; nothing said it was a list of names.

    Read off the annotation rather than written beside each field, for
    `_catalogue_block`'s reason: a prompt carrying its own copy of the schema is
    a prompt that falls behind the schema without anything going red.
    """
    notes = []
    for field, declared in config_model_of(node_type).model_fields.items():
        shape = _shape_of(declared.annotation)
        if is_resource_field(node_type, field):
            notes.append(f'"{field}" ({shape} — hệ thống tự điền, bạn để trống)')
        elif is_prose_field(node_type, field):
            notes.append(f'"{field}" ({shape} — {declared.description}, bạn viết)')
        else:
            notes.append(f'"{field}" ({shape} — {declared.description})')
    return notes


def _shape_of(annotation: object) -> str:
    """The JSON shape of one config field, in the words a JSON author uses.

    Enums are spelled as the values themselves rather than as "chuỗi", because
    for those the shape *is* the list: a field with two legal strings is not
    usefully described as a string.
    """
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return " hoặc ".join(f'"{member.value}"' for member in annotation)
    if get_origin(annotation) is list:
        return "mảng chuỗi, ví dụ []"
    if annotation is bool:
        return "true/false"
    if annotation is int:
        return "số nguyên"
    return "chuỗi"


def _models_block(model_allowlist: Sequence[str]) -> str:
    """The models a composed node may name, with price and tier — `generation.models_block`'s brief.

    Both facts, for the reason the V1 spec generator states: a model told only
    which models exist has no basis for putting the cheap one on the extraction
    step, which is the whole cost argument for assigning per node.

    An allowlist with nothing assignable in it produces a line saying so rather
    than an empty block. It is a state an admin can really configure, and
    `default_node_model` already answers it with a blank the canvas rings — a
    prompt that simply showed no models would leave the composer inventing one.

    **Cheapest first, and the order is load-bearing.** It used to be the
    allowlist's own order, which is `MODEL_CATALOG`'s order, which is the order
    somebody happened to type the entries in — and there `gpt-4o` ($2.50/$10.00)
    sits *above* `gpt-4.1` ($2.00/$8.00). Measured end to end after the
    cheap-by-default rule was already in the prompt: the composer obeyed it for
    five of six nodes and then, for the one node it judged to need a strong
    model, reached for the first strong entry it had read — the dearer of two
    models where the cheaper one is also the newer and the better. A list read
    top-down is a list of preferences whether or not it was written as one, so it
    is now written as one.
    """
    allowed = [name for name in model_allowlist if name in ASSIGNABLE_MODELS]
    if not allowed:
        return '- (nền tảng chưa cho phép model nào — để "model": "", người tạo sẽ tự chọn trên canvas)'
    cheapest_first = sorted(allowed, key=lambda name: _price_of(ASSIGNABLE_MODELS[name]))
    return "\n".join(f'- "{name}" — {_model_note(ASSIGNABLE_MODELS[name])}' for name in cheapest_first)


def _price_of(info: ModelInfo) -> tuple[float, float]:
    """What a model costs, output rate first.

    Output before input because output is the dearer half of every real bill —
    a composition this session measured at 2,859 tokens in and 1,040 out spent
    59% of its money on the 1,040.
    """
    return info.usd_per_million_output_tokens, info.usd_per_million_input_tokens


def _model_note(info: ModelInfo) -> str:
    tier = "model rẻ, hợp với trích xuất/phân loại" if info.tier is ModelTier.CHEAP else "model mạnh"
    return (
        f"{tier}; {info.usd_per_million_input_tokens}$ / 1M token vào, "
        f"{info.usd_per_million_output_tokens}$ / 1M token ra"
    )


def _all_resource_fields() -> set[str]:
    """Every field name across the eight types that addresses a creator's own resource."""
    return {
        field
        for node_type in NODE_REGISTRY
        for field in config_model_of(node_type).model_fields
        if is_resource_field(node_type, field)
    }
