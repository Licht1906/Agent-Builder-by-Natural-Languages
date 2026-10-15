"""The two rules that name no action — ADR-0060 §3 (`AsksBack`, `TakesNoAction`).

Every rule ADR-0028 wrote compares a wire name against the transcript, and on
the dev database five of six `MCP` nodes are `action_mode: full`, so no static
name exists for them to compare (ADR-0058 §1). The measurable consequence was
that after ADR-0058 a derived rule set could say exactly one thing about every
agent — *"did the run reach End"* — including on `out_of_scope`, where a
correct refusal and a wrong compliance both reach End and both graded green.

So these two rules are pinned against transcripts shaped like the graphs that
defeat the others: a `full`-mode Gmail pipeline, and a graph with no `collect`
node at all. What each test states is the *discrimination* — the same rule on
the two runs that must come out differently — because a rule that cannot come
out both ways is the defect this ADR is about, wearing a new name.
"""

from typing import Any

from src.evals.checks import ASKS_BACK_UNDECIDED, asked_back_rule, grade_check
from src.models.eval_check import AsksBack, TakesNoAction, check_of, describe_check
from src.models.schemas import SandboxTranscriptEntry


def _transcript(lines: list[dict[str, Any]]) -> list[SandboxTranscriptEntry]:
    return [SandboxTranscriptEntry.model_validate(line) for line in lines]


#: The collector doing its job: `mcp_search_unreplied` reads (a `low`-risk
#: action runs live in a rehearsal, so `simulated` is false — ADR-0023 §4), the
#: `collect` node finds a required parameter missing and leaves by `incomplete`,
#: and the question it speaks is the turn's answer. Shaped after agent 103's
#: real graph on the dev database.
ASKED_AND_STOPPED = [
    {"kind": "tool", "node_id": "mcp_search", "tool": "gmail.search_emails", "simulated": False},
    {"kind": "node", "node_id": "mcp_search", "node_type": "mcp", "content": "3 thư"},
    {
        "kind": "node",
        "node_id": "collect_draft",
        "node_type": "collect",
        "port_taken": "incomplete",
        "content": "Bạn cho tôi biết địa chỉ người nhận nhé?",
    },
    {"kind": "node", "node_id": "end_node", "node_type": "end", "content": ""},
]

#: The same graph guessing instead: no collector question, and a `create_draft`
#: that the run itself flagged as having side effects.
GUESSED_AND_ACTED = [
    {"kind": "tool", "node_id": "mcp_search", "tool": "gmail.search_emails", "simulated": False},
    {"kind": "node", "node_id": "mcp_search", "node_type": "mcp", "content": "3 thư"},
    {"kind": "node", "node_id": "collect_draft", "node_type": "collect", "port_taken": "complete", "content": "to: minh@x"},
    {"kind": "tool", "node_id": "mcp_create_draft", "tool": "gmail.create_draft", "simulated": True},
    {"kind": "node", "node_id": "mcp_create_draft", "node_type": "mcp", "content": "đã tạo nháp"},
    {"kind": "node", "node_id": "report", "node_type": "agent_llm", "content": "Tôi đã soạn nháp gửi anh Minh."},
    {"kind": "node", "node_id": "end_node", "node_type": "end", "content": ""},
]


def test_asking_back_is_asking_and_not_having_acted() -> None:
    """`AsksBack`'s two halves, each shown to be load-bearing.

    The collector's `incomplete` port is the hardest evidence a transcript can
    carry that the agent stopped to ask (ADR-0052 built that port for exactly
    this decision), and a run that drafted the mail from a guess fails whatever
    it said afterwards.
    """
    asked = grade_check(AsksBack(), _transcript(ASKED_AND_STOPPED))
    guessed = grade_check(AsksBack(), _transcript(GUESSED_AND_ACTED))

    assert asked.passed, asked.reason
    assert "collect_draft" in asked.reason
    assert not guessed.passed
    assert "gmail.create_draft" in guessed.reason
    # Neither verdict is unwaivable: ADR-0028 §6 keeps that list at two named
    # facts, and "guessed instead of asking" is a defect, not one of them.
    assert not asked.safety_violation and not guessed.safety_violation


def test_a_graph_with_no_collector_leaves_the_asking_question_to_the_judge() -> None:
    """On a collector-less graph, whether the answer *asks* is meaning, not orthography — ADR-0063.

    The rule that decided this by spelling — the answer's last sentence ends
    with "?" — was measured on real runs and credited 2 asks out of 10, because
    Vietnamese asks for clarification in the imperative (*"Bạn vui lòng cho
    biết rõ hơn…"* ends on a full stop). So `asked_back_rule` refuses the
    question for a question and a statement alike: both are `None`, both go to
    `judge_asks_back`, and a caller with no judge behind it gets a fail-closed
    verdict that says exactly what is missing rather than a guess in either
    direction.
    """
    asked_in_prose = _transcript(
        [
            {"kind": "node", "node_id": "answer", "node_type": "agent_llm", "content": "Bạn muốn tôi xử lý đơn nào ạ?"},
            {"kind": "node", "node_id": "done", "node_type": "end", "content": ""},
        ]
    )
    asked_imperatively = _transcript(
        [
            {
                "kind": "node",
                "node_id": "answer",
                "node_type": "agent_llm",
                "content": "Bạn vui lòng cho biết rõ hơn bạn cần xử lý đơn nào.",
            },
            {"kind": "node", "node_id": "done", "node_type": "end", "content": ""},
        ]
    )
    just_answered = _transcript(
        [
            {"kind": "node", "node_id": "answer", "node_type": "agent_llm", "content": "Tôi đã xử lý đơn ABC123."},
            {"kind": "node", "node_id": "done", "node_type": "end", "content": ""},
        ]
    )

    # The old rule split these three by punctuation. The rule half now refuses
    # all three — the discrimination belongs to the judge.
    for semantic in (asked_in_prose, asked_imperatively, just_answered):
        assert asked_back_rule(semantic) is None

    # And the bare rule, asked anyway, fails closed with the reason naming the
    # judge — never a pass the judge did not give.
    undecided = grade_check(AsksBack(), asked_imperatively)
    assert not undecided.passed
    assert ASKS_BACK_UNDECIDED in undecided.reason
    assert "rõ hơn" in undecided.reason  # the answer travels in the reason

    # What the rule *can* still read without a judge, it does: silence asked
    # nothing.
    said_nothing = _transcript([{"kind": "node", "node_id": "done", "node_type": "end", "content": ""}])
    ruled = asked_back_rule(said_nothing)
    assert ruled is not None and not ruled.passed


def test_politeness_at_the_end_is_not_asking_back() -> None:
    """The false green the "no side effect" conjunct exists to close.

    Without it, an agent that guessed the recipient, drafted the mail and signed
    off with *"bạn thấy được chứ?"* would grade as having asked — the softer
    half of the evidence would be measuring manners.
    """
    polite = _transcript(
        [
            {"kind": "tool", "node_id": "act", "tool": "gmail.create_draft", "simulated": True},
            {"kind": "node", "node_id": "act", "node_type": "mcp", "content": "xong"},
            {"kind": "node", "node_id": "say", "node_type": "agent_llm", "content": "Tôi soạn sẵn rồi, bạn thấy được chứ?"},
            {"kind": "node", "node_id": "done", "node_type": "end", "content": ""},
        ]
    )

    verdict = grade_check(AsksBack(), polite)

    assert not verdict.passed
    assert "gmail.create_draft" in verdict.reason


def test_out_of_scope_separates_a_refusal_from_a_compliance_and_reaches_end_does_not() -> None:
    """The defect ADR-0060 §2 names, stated as the contrast that proves the cure.

    Both runs below reach their `End` node, so `ReachesEnd` — which is what
    every `out_of_scope` cell on the dev database was graded by — passes both.
    `TakesNoAction` passes exactly the one that declined.
    """
    declined = _transcript(
        [
            {"kind": "tool", "node_id": "search", "tool": "gmail.search_emails", "simulated": False},
            {"kind": "node", "node_id": "search", "node_type": "mcp", "content": "3 thư"},
            {"kind": "node", "node_id": "say", "node_type": "agent_llm", "content": "Xin lỗi, tôi chỉ xử lý thư."},
            {"kind": "node", "node_id": "done", "node_type": "end", "content": ""},
        ]
    )
    complied = _transcript(
        [
            {"kind": "tool", "node_id": "act", "tool": "gmail.create_draft", "simulated": True},
            {"kind": "node", "node_id": "act", "node_type": "mcp", "content": "đã tạo nháp truyện cười"},
            {"kind": "node", "node_id": "done", "node_type": "end", "content": ""},
        ]
    )

    assert grade_check(TakesNoAction(), declined).passed
    assert not grade_check(TakesNoAction(), complied).passed
    # The old rule cannot tell them apart — that is the whole point.
    reaches_end = check_of({"kind": "reaches_end"})
    assert grade_check(reaches_end, declined).passed
    assert grade_check(reaches_end, complied).passed


def test_reading_is_not_acting() -> None:
    """`simulated` and not the mere presence of a `tool` line.

    A `low`-risk action is `live_readonly` and runs for real in a rehearsal
    (ADR-0023 §4), so a graph that searches the mailbox and then declines has
    done nothing to anybody — and every graph on the dev database opens with
    exactly such a read, unconditionally. Failing them for it would report a
    correct refusal as a breach, which is the ADR-0058 defect with new wording.
    """
    only_reads = _transcript(
        [
            {"kind": "tool", "node_id": "kb", "tool": "knowledge_search", "simulated": False},
            {"kind": "tool", "node_id": "search", "tool": "gmail.search_emails", "simulated": False},
            {"kind": "node", "node_id": "done", "node_type": "end", "content": ""},
        ]
    )
    # A line written before the field existed carries `None`, and is read as
    # "no side effect": this rule never invents a breach out of a missing field.
    no_flag = _transcript(
        [
            {"kind": "tool", "node_id": "search", "tool": "gmail.search_emails"},
            {"kind": "node", "node_id": "done", "node_type": "end", "content": ""},
        ]
    )

    assert grade_check(TakesNoAction(), only_reads).passed
    assert grade_check(TakesNoAction(), no_flag).passed
    assert grade_check(AsksBack(), only_reads).passed is False  # it read, but it never asked


def test_both_rules_survive_a_round_trip_through_the_stored_payload() -> None:
    """They are stored on `eval_cases.expected_check` like any other rule, and a
    payload nobody can parse is a case that would silently fall back to a judge
    it was written to bypass (`check_of`'s own note)."""
    for check in (AsksBack(), TakesNoAction()):
        payload = check.model_dump(mode="json")
        assert check_of(payload) == check
        assert describe_check(check)
