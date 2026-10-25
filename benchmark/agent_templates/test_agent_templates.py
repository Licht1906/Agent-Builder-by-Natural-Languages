"""Mỗi fixture trong `templates/` vẫn là một tài liệu nền tảng này chạy được.

Không có lời gọi mạng nào ở đây và cũng không cần backend chạy: đây là nửa **tĩnh**
của bộ benchmark — spec có validate được không, graph có qua nổi cửa xác nhận
không, và cấu trúc mà file tự khai có khớp với graph nó chứa không. Nửa **động** —
sandbox, eval, duyệt, deploy — là `run_fleet.py`, và nó cần cả một hạ tầng đang
chạy nên không thuộc về một bài test.

Ba khẳng định, và cái thứ ba là cái đáng tiền: `structure` trong mỗi file là lời
hứa mà phần văn xuôi `why_hard` dựa vào. Một fixture bị sửa graph mà quên sửa
`structure` là một mục benchmark nói một đằng đo một nẻo, và đó chính là khuyết
tật mà ADR-0058 §1 đặt tên — một sự thật có hai cách đánh vần.
"""

import json
import pathlib

import pytest

from src.models.node_registry import NodeType
from src.models.solution_spec import AgentSolutionSpecification
from src.models.workflow import WorkflowJSON
from src.workflow.validation import graph_problems

TEMPLATES = pathlib.Path(__file__).resolve().parent / "templates"

#: Knowledge Base id thật chỉ có ở lúc chạy. Ở đây thay bằng một id nào cũng
#: được: `graph_problems` chỉ hỏi `source_ids` có rỗng không, còn "kệ này có
#: thật và có thuộc chủ agent không" là câu hỏi của `ensure_knowledge_bases_owned`
#: và nó cần database — đúng phần mà `run_fleet.py` đi qua.
PLACEHOLDER_KB = "1"


def fixtures() -> list[pathlib.Path]:
    return sorted(TEMPLATES.glob("*.json"))


def load(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8").replace("{{KB_ID}}", PLACEHOLDER_KB))


@pytest.mark.parametrize("path", fixtures(), ids=lambda p: p.stem)
def test_spec_validates(path: pathlib.Path) -> None:
    AgentSolutionSpecification.model_validate(load(path)["spec"])


@pytest.mark.parametrize("path", fixtures(), ids=lambda p: p.stem)
def test_graph_would_confirm(path: pathlib.Path) -> None:
    workflow = WorkflowJSON.model_validate(load(path)["workflow"])
    assert [problem.message for problem in graph_problems(workflow)] == []


@pytest.mark.parametrize("path", fixtures(), ids=lambda p: p.stem)
def test_structure_block_matches_the_graph(path: pathlib.Path) -> None:
    fixture = load(path)
    workflow = WorkflowJSON.model_validate(fixture["workflow"])
    structure = fixture["structure"]

    assert len(workflow.nodes) == structure["node_count"]
    assert len(workflow.edges) == structure["edge_count"]

    for node_id, claim in structure["must_have_nodes"].items():
        node = workflow.node(node_id)
        assert node is not None, f"{path.name}: structure nói tới node {node_id!r} mà graph không có"
        assert node.type.value == claim["type"]
        for field in ("tool_id", "action", "action_mode", "require_approval", "action_node", "max_iterations"):
            if field in claim:
                assert node.config.get(field) == claim[field], f"{node_id}.{field}"
        if "agent_writes" in claim:
            assert node.config.get("agent_writes") == claim["agent_writes"]
        if claim.get("condition_non_empty"):
            assert str(node.config.get("condition", "")).strip()
        if claim.get("prompt_non_empty"):
            assert str(node.config.get("prompt", "")).strip()
        if "source_ids_len" in claim:
            assert len(node.config.get("source_ids") or []) == claim["source_ids_len"]
        for port in claim.get("ports_wired", ()):
            assert any(
                edge.source_node_id == node_id and edge.source_port == port for edge in workflow.edges
            ), f"{node_id}: cổng {port!r} chưa nối đi đâu"


@pytest.mark.parametrize("path", fixtures(), ids=lambda p: p.stem)
def test_capabilities_are_actually_in_the_graph(path: pathlib.Path) -> None:
    """`capabilities` là cái tủ kính của mục benchmark — nó không được nói quá.

    Một mục khai `human_approval` mà graph không có node nào loại đó là đúng lời
    nói dối mà cả gói việc này sinh ra để chấm dứt: agent 105 tên là "Gmail có
    duyệt" và trong graph của nó không có một node `human_approval` nào.
    """
    fixture = load(path)
    workflow = WorkflowJSON.model_validate(fixture["workflow"])
    present = {node.type for node in workflow.nodes}
    required = {
        "human_approval": NodeType.HUMAN_APPROVAL,
        "collect": NodeType.COLLECT,
        "if_else": NodeType.IF_ELSE,
        "while": NodeType.WHILE,
        "knowledge_search": NodeType.KNOWLEDGE_SEARCH,
    }
    for capability in fixture["capabilities"]:
        node_type = required.get(capability)
        if node_type is not None:
            assert node_type in present, f"{path.name} khai {capability!r} mà graph không có node loại đó"


def test_the_fleet_covers_every_node_type_it_can() -> None:
    """Tám trên chín loại node phải xuất hiện đâu đó trong bộ này.

    `web_search` là loại thứ chín và cố tình vắng: nó đi tới bản cài đặt thật và
    bản cài đặt thật đòi `SEARCH_API_KEY`, không có khoá thì node ghi một dòng
    từ chối vào transcript. Một mục benchmark mà bước giữa luôn hỏng thì không đo
    được gì về agent — thêm nó vào là thêm một ô luôn đỏ.
    """
    seen: set[NodeType] = set()
    for path in fixtures():
        workflow = WorkflowJSON.model_validate(load(path)["workflow"])
        seen |= {node.type for node in workflow.nodes}
    assert set(NodeType) - seen == {NodeType.WEB_SEARCH}
