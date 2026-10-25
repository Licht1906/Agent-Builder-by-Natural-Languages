"""A trial the platform broke is not evidence about the agent — ADR-0061.

Every transcript shape below is one this platform actually wrote. The two
sentences at the top were read off `sandbox_runs` 100 and 110 of agent 103
version 127 (the run that produced the four false reds and the two false
greens ADR-0061 was opened for), and the business-error line is run 105's:
`read_email` refused for a missing `message_id`, which is the world being
unhelpful and is graded, not excused.

ADR-0065 retired the scenario grid, which is where a void trial used to be
*tallied*. What it did not retire is the rule these tests state: **where the
line sits between a run the agent failed and a run nobody got to grade**, and
the order that keeps a safety violation from being waived by a broken
credential. That rule is `src.evals.faults`, it is reached from the ordinary
suite's runner, and it is what survives here. The four tests that drove
`run_evidence_cells` over `evidence_cells` rows went with the grid.
"""

from src.evals.checks import CheckVerdict
from src.evals.faults import fault_of_run_error, observation_of, trial_fault
from src.models.node_registry import McpConfig
from src.models.schemas import SandboxTranscriptEntry
from src.workflow.faults import (
    EXPIRED_PASTE_MESSAGE,
    PlatformFault,
    fault_of_exception,
    fault_of_reason,
)
from src.workflow.google_tools import GoogleToolError
from src.workflow.runtime import _unresolved_fault
from src.workflow.tools import ToolUnavailableError
from src.workflow.user_connections import UserConnectionError

#: `sandbox_runs` 100, line 2 — the fault that voided all forty trials.
EXPIRED = f"Không thực hiện được hành động 'search_emails': {EXPIRED_PASTE_MESSAGE}"
#: `sandbox_runs` 100, line 8 — the `full`-mode fallback, and **not** a fault.
FULL_MODE_FALLBACK = "Không có tool 'gmail' với action '' trong registry"
#: `sandbox_runs` 105 — the world being unhelpful, which is graded.
BUSINESS_ERROR = (
    "Không thực hiện được hành động 'read_email': read_email cần message_id — "
    "lấy từ kết quả search_emails/get_recent_emails"
)


def _tool_result(content: str, *, ok: bool = False, fault: str | None = None) -> SandboxTranscriptEntry:
    return SandboxTranscriptEntry(
        kind="tool_result", node_id="mcp_search", tool="gmail.search_emails", ok=ok, simulated=False,
        content=content, fault=fault,
    )


# --------------------------------------------------------------------------- #
# Where the line sits
# --------------------------------------------------------------------------- #


def test_a_credential_that_died_voids_the_trial_but_a_tool_that_answered_badly_does_not() -> None:
    """The boundary, on the two sentences the same run wrote one after the other."""
    assert trial_fault([_tool_result(EXPIRED, fault=PlatformFault.CREDENTIAL.value)]) is PlatformFault.CREDENTIAL
    assert trial_fault([_tool_result(BUSINESS_ERROR)]) is None


def test_a_tool_that_succeeded_or_found_nothing_is_never_a_fault() -> None:
    """`ok=True` is not read at all — an empty result is material, not a fault."""
    assert trial_fault([_tool_result("[]", ok=True)]) is None
    assert trial_fault([_tool_result("Không tìm thấy thư nào", ok=True)]) is None


def test_the_first_fault_in_the_transcript_is_the_one_reported() -> None:
    """The creator has to fix the thing that stopped the run first."""
    transcript = [
        _tool_result(EXPIRED, fault=PlatformFault.CREDENTIAL.value),
        _tool_result("bất kỳ", fault=PlatformFault.TOOL_UNAVAILABLE.value),
    ]
    assert trial_fault(transcript) is PlatformFault.CREDENTIAL


def test_a_run_level_provider_failure_voids_and_nothing_else_does() -> None:
    import httpx
    import openai

    assert fault_of_run_error(httpx.ConnectError("down")) is PlatformFault.MODEL
    assert fault_of_run_error(openai.APIError("x", request=None, body=None)) is PlatformFault.MODEL
    # The exclusions are the point: an agent that looped until its ceiling, and
    # a graph that raised, are failures somebody has to see.
    assert fault_of_run_error(RuntimeError("Đã vượt trần token của lần chạy")) is None
    assert fault_of_run_error(ValueError("node type không hỗ trợ")) is None
    assert trial_fault([], run_fault=PlatformFault.MODEL) is PlatformFault.MODEL


def test_a_transcript_line_that_carries_no_fault_and_no_known_sentence_is_the_agents() -> None:
    """A tool result is innocent until the platform says otherwise."""
    assert trial_fault([_tool_result("Gmail nói: quota exceeded for this mailbox")]) is None


# --------------------------------------------------------------------------- #
# The stamp: written where it is known, read where it is used
# --------------------------------------------------------------------------- #


def test_the_fault_survives_being_re_raised_as_another_exception_type() -> None:
    """`google_tools` re-raises a credential error as a `GoogleToolError`; the fact must not be lost."""
    try:
        try:
            raise UserConnectionError(EXPIRED_PASTE_MESSAGE)
        except UserConnectionError as exc:
            raise GoogleToolError(str(exc)) from exc
    except GoogleToolError as outer:
        assert fault_of_exception(outer) is PlatformFault.CREDENTIAL

    assert fault_of_exception(ToolUnavailableError("chưa nối")) is PlatformFault.TOOL_UNAVAILABLE
    # A Google refusal about the *world* carries nothing, and must not.
    assert fault_of_exception(GoogleToolError("Google nói không tồn tại: message id")) is None
    assert fault_of_exception(None) is None


def test_a_full_mode_node_that_the_model_declined_to_use_is_not_a_platform_fault() -> None:
    """Agent 103's `mcp_create_draft`, exactly as stored — and the reason it is graded, not excused.

    `_filled_by_model` binds the model to the connection's real actions and
    drops a reply naming anything else, so arriving at the empty fallback means
    the model was offered working actions and picked none. Calling that a fault
    would let any agent void its own trial by declining to act.
    """
    stored = McpConfig.model_validate(
        {"tool_id": "gmail", "action": "", "action_mode": "full", "allowed_actions": [], "require_approval": "always"}
    )
    assert _unresolved_fault(stored) is None


def test_a_pinned_action_the_registry_lacks_and_an_unknown_connection_are_faults() -> None:
    pinned = McpConfig.model_validate({"tool_id": "gmail", "action": "khong_co_that", "action_mode": "single"})
    unknown = McpConfig.model_validate({"tool_id": "khong_ai_dang_ky", "action": "x", "action_mode": "single"})
    narrowed = McpConfig.model_validate(
        {"tool_id": "gmail", "action": "", "action_mode": "full", "allowed_actions": ["khong_co_that"]}
    )

    assert _unresolved_fault(pinned) is PlatformFault.ACTION_UNRESOLVED
    assert _unresolved_fault(unknown) is PlatformFault.TOOL_UNAVAILABLE
    assert _unresolved_fault(narrowed) is PlatformFault.ACTION_UNRESOLVED


def test_records_written_before_the_field_existed_are_read_from_the_platforms_own_sentences() -> None:
    """Every one of the forty stored trials predates `SandboxTranscriptEntry.fault`."""
    assert fault_of_reason(EXPIRED) is PlatformFault.CREDENTIAL
    assert fault_of_reason("Không có tool 'gmail' với action 'khong_co_that' trong registry") is (
        PlatformFault.ACTION_UNRESOLVED
    )
    assert fault_of_reason("Tool 'gmail' đang ở trạng thái 'disabled' nên không được gọi") is (
        PlatformFault.TOOL_UNAVAILABLE
    )
    # The two the old reader must NOT claim: the `full`-mode fallback, and a
    # tool that ran and refused.
    assert fault_of_reason(FULL_MODE_FALLBACK) is None
    assert fault_of_reason(BUSINESS_ERROR) is None
    # And an old line is read that way only because it carries no stamp.
    assert trial_fault([_tool_result(EXPIRED)]) is PlatformFault.CREDENTIAL
    assert trial_fault([_tool_result(FULL_MODE_FALLBACK)]) is None


# --------------------------------------------------------------------------- #
# The order: void may never swallow a rule nobody can waive
# --------------------------------------------------------------------------- #


def test_a_safety_violation_outranks_a_platform_fault() -> None:
    """A run that called a forbidden action still called it (ADR-0028 §6)."""
    observation = observation_of(
        verdict=CheckVerdict(passed=False, reason="đã gửi thư sau khi bị từ chối", safety_violation=True),
        transcript=[_tool_result(EXPIRED, fault=PlatformFault.CREDENTIAL.value)],
        error=None,
        run_fault=None,
        answer="",
    )
    assert observation.void is None
    assert observation.hard_failure is True


def test_a_fault_outranks_the_verdict_and_the_verdict_outranks_nothing_being_said() -> None:
    transcript = [_tool_result(EXPIRED, fault=PlatformFault.CREDENTIAL.value)]
    voided = observation_of(
        verdict=CheckVerdict(passed=True, reason="không làm gì"), transcript=transcript,
        error=None, run_fault=None, answer="xong",
    )
    graded = observation_of(
        verdict=CheckVerdict(passed=True, reason="không làm gì"), transcript=[],
        error=None, run_fault=None, answer="xong",
    )
    stopped = observation_of(verdict=None, transcript=[], error="Agent không chạy xong", run_fault=None, answer="")

    assert (voided.void, voided.passed) == (PlatformFault.CREDENTIAL, False)
    assert (graded.void, graded.passed) == (None, True)
    assert (stopped.void, stopped.passed) == (None, False)
