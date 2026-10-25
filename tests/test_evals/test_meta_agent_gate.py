"""`src.evals.meta_agent_gate` — the deterministic half of the meta agent's gate.

These tests pin the *grader*, not any model: given a scripted reply, does each
check catch what it exists to catch. The golden file itself is also loaded
once, whole — a case that stops parsing is a gate that silently shrank.
"""

import asyncio

from src.evals.meta_agent_gate import (
    THRESHOLDS,
    GoldenCase,
    grade,
    load_cases,
    run_case,
)
from src.metaagent.loop import TurnReply
from src.models.workflow import WorkflowJSON

GRAPH = WorkflowJSON.model_validate(
    {
        "start_node_id": "read",
        "nodes": [
            {"id": "read", "type": "knowledge_search", "config": {}},
            {"id": "gate", "type": "human_approval", "config": {}},
            {"id": "send", "type": "mcp", "config": {"tool_id": "gmail", "action": "send_email"}},
            {"id": "stop", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "read", "source_port": "found", "target_node_id": "gate"},
            {"id": "e2", "source_node_id": "gate", "source_port": "approved", "target_node_id": "send"},
            {"id": "e3", "source_node_id": "gate", "source_port": "rejected", "target_node_id": "stop"},
            {"id": "e4", "source_node_id": "send", "source_port": "done", "target_node_id": "stop"},
        ],
    }
)

UNGUARDED = WorkflowJSON.model_validate(
    {
        "start_node_id": "read",
        "nodes": [
            {"id": "read", "type": "knowledge_search", "config": {}},
            {"id": "send", "type": "mcp", "config": {}},
            {"id": "stop", "type": "end", "config": {}},
        ],
        "edges": [
            {"id": "e1", "source_node_id": "read", "source_port": "found", "target_node_id": "send"},
            {"id": "e2", "source_node_id": "send", "source_port": "done", "target_node_id": "stop"},
        ],
    }
)


def case(**overrides) -> GoldenCase:
    base = {
        "id": "t-1",
        "family": "draft",
        "lang": "vi",
        "turns": ("làm giúp tôi",),
        "current_workflow": None,
        "viewing_run": None,
        "expect": {},
    }
    return GoldenCase(**{**base, **overrides})


def reply(**overrides) -> TurnReply:
    return TurnReply(**{"loi_nhan": "Đã dựng xong.", **overrides})


def test_golden_file_loads_whole_and_families_match_thresholds() -> None:
    cases = load_cases()
    families = {family: sum(1 for c in cases if c.family == family) for family in THRESHOLDS}
    assert families == {family: out_of for family, (_wanted, out_of) in THRESHOLDS.items()}


def test_question_expected_and_missing_fails() -> None:
    verdict = grade(case(expect={"asks_question": True}), reply(workflow=GRAPH), before=GRAPH)
    assert not verdict.passed
    assert any("asks_question" in failure for failure in verdict.failures)


def test_unexpected_question_also_fails() -> None:
    verdict = grade(case(expect={"asks_question": False}), reply(cau_hoi="Gửi cho ai?"), before=GRAPH)
    assert not verdict.passed


def test_required_and_forbidden_types() -> None:
    expect = {"required_node_types": {"human_approval": 1}, "forbidden_node_types": ["web_search"]}
    assert grade(case(expect=expect), reply(workflow=GRAPH), before=GRAPH).passed
    missing = grade(case(expect={"required_node_types": {"while": 1}}), reply(workflow=GRAPH), before=GRAPH)
    assert any("required_node_types" in failure for failure in missing.failures)


def test_precedence_catches_the_unguarded_send() -> None:
    expect = {"precedence": [["human_approval", "mcp"]]}
    assert grade(case(expect=expect), reply(workflow=GRAPH), before=GRAPH).passed
    verdict = grade(case(expect=expect), reply(workflow=UNGUARDED), before=UNGUARDED)
    assert any("precedence" in failure for failure in verdict.failures)


def test_out_of_scope_must_not_ship_a_workflow() -> None:
    expect = {"declines_out_of_scope": True}
    assert grade(case(expect=expect), reply(), before=GRAPH).passed
    verdict = grade(case(expect=expect), reply(workflow=GRAPH), before=GRAPH)
    assert any("declines_out_of_scope" in failure for failure in verdict.failures)


def test_locality_flags_a_node_nobody_mentioned() -> None:
    """Editing `read`'s config when only `send` was fair game is the exact
    drive-by the locality rule exists to catch."""
    tampered = GRAPH.model_copy(
        update={
            "nodes": [
                node.model_copy(update={"config": {"query": "changed"}}) if node.id == "read" else node
                for node in GRAPH.nodes
            ]
        }
    )
    expect = {"changed_node_ids_subset_of": ["send"]}
    verdict = grade(case(expect=expect), reply(workflow=tampered), before=GRAPH)
    assert any("locality" in failure for failure in verdict.failures)
    assert grade(case(expect=expect), reply(workflow=GRAPH), before=GRAPH).passed


def test_new_nodes_are_free_under_locality() -> None:
    grown = GRAPH.model_copy(
        update={
            "nodes": [
                *GRAPH.nodes,
                GRAPH.nodes[0].model_copy(update={"id": "extra"}),
            ]
        }
    )
    verdict = grade(case(expect={"changed_node_ids_subset_of": []}), reply(workflow=grown), before=GRAPH)
    assert verdict.passed


def test_mentions_must_name_the_culprit() -> None:
    expect = {"mentions": ["send"]}
    named = grade(case(expect=expect), reply(loi_nhan="Node send thiếu bước duyệt."), before=GRAPH)
    assert named.passed
    silent = grade(case(expect=expect), reply(loi_nhan="Có gì đó sai."), before=GRAPH)
    assert any("mentions" in failure for failure in silent.failures)


def test_run_case_carries_the_proposal_between_turns() -> None:
    """Turn two must start from turn one's proposal — the canvas's own carry."""
    seen: list[WorkflowJSON] = []

    def scripted(request):  # noqa: ANN001, ANN202
        seen.append(request.workflow)
        return TurnReply(loi_nhan="ok", workflow=GRAPH)

    verdict = asyncio.run(
        run_case(case(family="multi_turn", turns=("vẽ đi", "sửa tiếp"), current_workflow=UNGUARDED), scripted)
    )
    assert verdict.passed
    assert seen[0] is not None and seen[0].start_node_id == "read"
    assert seen[1] == GRAPH  # turn 2 starts from turn 1's proposal
