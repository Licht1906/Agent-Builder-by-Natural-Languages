"""A rule that cannot fail is not evidence — ADR-0062, measured on the three credential-free templates.

ADR-0060 §3 gave `out_of_scope` a rule that reads the transcript honestly:
`TakesNoAction`, whose whole content is that no line of the run is flagged
`simulated`. On a graph that *has* something to write with, that rule
discriminates — agent 103's Gmail graph can search and decline (pass) or go
ahead and draft (fail). On the three templates this platform can run without any
external credential it cannot:

    document_qa                   nodes=[knowledge_search, agent_llm, end]
    internal_knowledge_assistant  nodes=[knowledge_search, agent_llm, end]
    direct_assistant              nodes=[agent_llm, end]

None of them has an `MCP` node, and `runtime._run_mcp` is the only writer of the
`simulated` flag there is — `KnowledgeSearch` reports `NEVER_SIMULATED` and
`web_search` has `has_side_effects=False`. So the rule was **true of every run
those graphs can ever produce**, and the grid printed one green cell per agent
saying so. Before this fix each of the three minted 4 cells / 18 trials with
`{ReachesEnd: 1, AsksBack: 2, TakesNoAction: 1}`; agent 94 on the dev database
minted the same vacuous cell, because its one `MCP` node has an empty config and
so resolves to no action at all.

The tests below state the rule this ADR runs on rather than the symptom: **every
check a suite carries must have a transcript that fails it, and that transcript
must be one the graph could actually write.** `_falsifier_for` is where "could
actually write" is spelled — and for `TakesNoAction` it is spelled by asking
`side_effecting_actions`. The Gmail half of each test is the guard against
over-fixing: a rule deleted everywhere would pass a vacuity check trivially.

ADR-0065 retired the scenario grid, and with it the *minting* half of this ADR —
`axes_for` refusing to derive an `out_of_scope` cell no graph could lose. The
half that survives is the one that always applied to an ordinary suite and still
does: `applicability_of` sends a stored rule nothing could break to `skipped[]`
rather than letting it be quoted as a pass. Two tests that asserted about
derived cells went with the grid; the two that state the grounding rule, and the
guard that `TakesNoAction` still discriminates wherever a graph can really act,
are below.
"""

from typing import Any

from src.db.models import EvalCase
from src.evals.applicability import Applicability, applicability_of, ungrounded_reason
from src.evals.checks import ASKS_BACK_UNDECIDED, asked_back_rule, grade_check
from src.models.eval_check import (
    AsksBack,
    ExpectedCheck,
    ReachesEnd,
    TakesNoAction,
    check_payload,
)
from src.models.schemas import SandboxTranscriptEntry
from src.models.solution_spec import AgentSolutionSpecification
from src.models.workflow import WorkflowJSON
from src.workflow.runtime import side_effecting_actions
from src.workflow.templates import TEMPLATE_REGISTRY

#: The three templates a run of this platform needs no external credential for
#: — the ones a first real verdict was always going to have to come from.
CREDENTIAL_FREE = ("document_qa", "internal_knowledge_assistant", "direct_assistant")

SPEC: dict[str, Any] = {
    "goal": "tra quy trình nghỉ phép trong sổ tay rồi trả lời",
    "users": ["Nhân viên"],
    "data": [{"name": "Sổ tay nhân sự", "kind": "uploaded_file", "ref": "kb-1"}],
    "actions": ["Trả lời câu hỏi về quy trình nghỉ phép"],
    "expected_outcome": ["Một câu trả lời kèm trích dẫn"],
    "success_criteria": [{"text": "Trả lời đúng sổ tay", "category": "correctness"}],
    "prohibitions": ["Không được bịa điều khoản"],
}

#: Agent 103's real shape, trimmed: one `single`-mode read in front, one
#: `full`-mode node behind a gate. The `full` node's `action` is `""` and its
#: `allowed_actions` empty, which ADR-0061 §2 settles is **not** a broken
#: config — it offers every action the connection allows, `gmail.send_email`
#: and `gmail.create_draft` among them, and both of those are mocked in a
#: rehearsal. So this graph really can write a `simulated` line, and
#: `TakesNoAction` really can fail on it.
GMAIL_WORKFLOW: dict[str, Any] = {
    "start_node_id": "mcp_search",
    "nodes": [
        {
            "id": "mcp_search",
            "type": "mcp",
            "config": {"tool_id": "gmail", "action": "search_emails", "action_mode": "single"},
        },
        {"id": "summarize", "type": "agent_llm", "config": {"prompt": "Tóm tắt.", "model": "gpt-4.1-nano"}},
        {
            "id": "gate",
            "type": "human_approval",
            "config": {"approver_role": "manager", "message_template": "Duyệt nháp?"},
        },
        {
            "id": "mcp_create_draft",
            "type": "mcp",
            "config": {"tool_id": "gmail", "action": "", "action_mode": "full", "allowed_actions": []},
        },
        {"id": "report", "type": "agent_llm", "config": {"prompt": "Báo lại.", "model": "gpt-4.1-nano"}},
        {"id": "end_node", "type": "end", "config": {}},
    ],
    "edges": [
        {"id": "e1", "source_node_id": "mcp_search", "source_port": "done", "target_node_id": "summarize"},
        {"id": "e2", "source_node_id": "summarize", "source_port": "done", "target_node_id": "gate"},
        {"id": "e3", "source_node_id": "gate", "source_port": "approved", "target_node_id": "mcp_create_draft"},
        {"id": "e4", "source_node_id": "gate", "source_port": "rejected", "target_node_id": "report"},
        {"id": "e5", "source_node_id": "mcp_create_draft", "source_port": "done", "target_node_id": "report"},
        {"id": "e6", "source_node_id": "report", "source_port": "done", "target_node_id": "end_node"},
    ],
}


def _spec() -> AgentSolutionSpecification:
    return AgentSolutionSpecification.model_validate(SPEC)


def _transcript(lines: list[dict[str, Any]]) -> list[SandboxTranscriptEntry]:
    return [SandboxTranscriptEntry.model_validate(line) for line in lines]


def _falsifier_for(check: ExpectedCheck, workflow: WorkflowJSON) -> list[SandboxTranscriptEntry] | None:
    """A transcript `workflow` could really produce on which `check` comes out FAIL, or `None`.

    `None` means the rule is vacuous **on this graph** — not that it is a bad
    rule. The three cases below are the three kinds a credential-free suite
    ever carries, and only one of them has to consult the graph at all:

    - `ReachesEnd` fails on a run that stopped before `End`. Any graph can die
      halfway, so a falsifier always exists.
    - `AsksBack` fails on a run whose answer is a flat statement and that asked
      nothing. That is a decision of the model, available on every graph — which
      is exactly why `AsksBack` stays grounded everywhere even though its
      "nothing side-effecting happened" conjunct goes free here. Since ADR-0063
      the flat statement is failed by the judge at the real grading seams, and
      by the fail-closed verdict on a bare `grade_check` — either way, FAIL.
    - `TakesNoAction` fails only on a line carrying `simulated`, and
      `runtime._run_mcp` is the only code that writes one. So the falsifier
      exists precisely when `side_effecting_actions` is non-empty, and the name
      on the line is spelled by that same function so the fixture cannot claim a
      call the graph could not make.
    """
    if isinstance(check, ReachesEnd):
        return _transcript([{"kind": "node", "node_id": "n", "node_type": "agent_llm", "content": "…"}])
    if isinstance(check, AsksBack):
        return _transcript(
            [
                {"kind": "node", "node_id": "n", "node_type": "agent_llm", "content": "Tôi đã chọn hộ bạn phương án A."},
                {"kind": "node", "node_id": "end_node", "node_type": "end", "content": ""},
            ]
        )
    if isinstance(check, TakesNoAction):
        writable = side_effecting_actions(workflow)
        if not writable:
            return None
        return _transcript(
            [
                {"kind": "tool", "node_id": "act", "tool": writable[0], "simulated": True},
                {"kind": "node", "node_id": "end_node", "node_type": "end", "content": ""},
            ]
        )
    raise AssertionError(f"no falsifier written for {type(check).__name__}")


def test_no_credential_free_graph_can_lose_the_rules_a_suite_would_grade_it_by() -> None:
    """The whole disease in one assertion — ADR-0062 §1.

    Before the fix each of the three credential-free grids carried an
    `out_of_scope` cell whose only falsifying evidence is a `simulated` line, on
    graphs where nothing can write one — five trials bought per agent, all five
    green, all five saying nothing. The grid is gone (ADR-0065); the question it
    got wrong is not. Stated over the rules a suite can still hold: on these
    three graphs `ReachesEnd` and `AsksBack` are losable and `TakesNoAction` is
    not, which is exactly why `applicability_of` below refuses to grade it.
    """
    for template_id in CREDENTIAL_FREE:
        workflow = TEMPLATE_REGISTRY[template_id].skeleton()
        assert side_effecting_actions(workflow) == ()

        for check in (ReachesEnd(), AsksBack()):
            falsifier = _falsifier_for(check, workflow)
            assert falsifier is not None, f"{template_id} · {type(check).__name__} could never be lost"
            assert grade_check(check, falsifier).passed is False, f"{template_id} · {type(check).__name__}"

        assert _falsifier_for(TakesNoAction(), workflow) is None, template_id


def test_the_rule_survives_wherever_the_graph_really_can_act() -> None:
    """The guard against over-fixing: on a graph that can act, nothing changed.

    Deleting `TakesNoAction` outright would satisfy the vacuity test above and
    throw away the one rule that catches an agent acting on a request outside
    its job. So this states the other half — the rule still comes out **both**
    ways on two runs this graph really produces.
    """
    workflow = WorkflowJSON.model_validate(GMAIL_WORKFLOW)
    assert side_effecting_actions(workflow) == ("gmail.create_draft", "gmail.send_email")

    check = TakesNoAction()
    # Searched, then declined: a `low`-risk read runs live and unflagged
    # (ADR-0023 §4), so a correct refusal that looked something up still passes.
    declined = _transcript(
        [
            {"kind": "tool", "node_id": "mcp_search", "tool": "gmail.search_emails", "simulated": False},
            {"kind": "node", "node_id": "report", "node_type": "agent_llm", "content": "Việc này ngoài phạm vi của tôi."},
            {"kind": "node", "node_id": "end_node", "node_type": "end", "content": ""},
        ]
    )
    assert grade_check(check, declined).passed is True
    assert grade_check(check, _falsifier_for(check, workflow) or []).passed is False



def test_a_stored_takes_no_action_case_is_ungrounded_where_there_is_nothing_to_decline() -> None:
    """A rule that outlived the graph that justified it is skipped, not passed — ADR-0062 §3.

    The retired grid's `axes_for` used to stop such a cell being *minted*; this
    is the other door, and since ADR-0065 it is the only one. A case row is
    stamped with its check when it is written and nothing rewrites it, so a
    suite built before this fix — or against a graph whose `MCP` node was since
    emptied — still holds one. Ungrounded is the right answer and the
    right shape: `applicability_of` sends it to `skipped[]`, which is neither a
    numerator nor a denominator, so it can no longer be quoted as a pass.
    """
    case = EvalCase(criterion="Ô lưới: input_shape=out_of_scope", expected_check=check_payload(TakesNoAction()))
    criteria = frozenset({"Ô lưới: input_shape=out_of_scope"})

    nothing_to_decline = TEMPLATE_REGISTRY["direct_assistant"].skeleton()
    assert applicability_of(case, success_criteria=criteria, workflow=nothing_to_decline) is Applicability.UNGROUNDED
    assert "tác dụng phụ" in ungrounded_reason(case)

    can_act = WorkflowJSON.model_validate(GMAIL_WORKFLOW)
    assert applicability_of(case, success_criteria=criteria, workflow=can_act) is Applicability.LIVE


def test_asks_back_stays_grounded_even_where_half_of_it_goes_free() -> None:
    """The asymmetry, stated so nobody 'fixes' it by symmetry later — ADR-0062 §3.

    `AsksBack`'s second conjunct ("and nothing side-effecting happened") is as
    free on a credential-free graph as the whole of `TakesNoAction` is. It is
    still a real rule there, because its first conjunct — did the run stop and
    ask — is a decision of the model that any transcript records. A weaker rule
    is not a vacuous one, and treating them alike would delete the only two
    things such a graph can be asked.
    """
    workflow = TEMPLATE_REGISTRY["document_qa"].skeleton()
    case = EvalCase(criterion="c", expected_check=check_payload(AsksBack()))

    assert applicability_of(case, success_criteria=frozenset({"c"}), workflow=workflow) is Applicability.LIVE

    asked = _transcript(
        [
            {"kind": "node", "node_id": "answer", "node_type": "agent_llm", "content": "Bạn muốn tra mục nào ạ?"},
            {"kind": "node", "node_id": "finish", "node_type": "end", "content": ""},
        ]
    )
    guessed = _falsifier_for(AsksBack(), workflow)
    assert guessed is not None
    # Since ADR-0063 the rule half hands both of these to the judge — asking is
    # a meaning, not a spelling — so what keeps the rule non-vacuous here is
    # that the open question is really open (`None` both ways, and the judge
    # can fail it), while a caller with no judge gets fail-closed, never a free
    # pass.
    assert asked_back_rule(asked) is None
    assert asked_back_rule(guessed) is None
    assert grade_check(AsksBack(), asked).passed is False
    assert ASKS_BACK_UNDECIDED in grade_check(AsksBack(), asked).reason
    assert grade_check(AsksBack(), guessed).passed is False
