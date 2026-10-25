"""UC1-04 — one test per rule the lifecycle state machine enforces.

Seam: the four named moves (`submit_for_approval`, `approve`, `deploy`,
`suspend`) plus `new_draft_version`. Nothing here reaches into `_with_status` or
the transition table directly — the functions *are* the state machine, same
argument as `test_validation.py` makes for `validate_spec`.

UC1-25 (#141) adds `rollback` — its own tests live separately from the
`test_each_move_is_refused_from_every_status_but_its_own` parametrize below
since `rollback` (unlike the other four moves) has *two* legal starting
statuses, not one.
"""

import pytest

from src.agentspec import AgentSpec, InvalidTransitionError, SpecStatus, SuspensionReason
from src.agentspec.lifecycle import approve, deploy, new_draft_version, rollback, submit_for_approval, suspend
from src.agentspec.spec import Evaluation
from tests.test_agentspec.conftest import spec_payload


def spec(**overrides) -> AgentSpec:
    payload = spec_payload(**overrides)
    return AgentSpec.model_validate(payload)


def refusal(fn, *args, **kwargs) -> InvalidTransitionError:
    with pytest.raises(InvalidTransitionError) as caught:
        fn(*args, **kwargs)
    return caught.value


def test_draft_advances_to_pending_approval_only_once_all_three_conditions_hold() -> None:
    """AC-adjacent: §13's combined condition for `draft -> pending_approval`.

    Missing more than one condition is reported in the same refusal rather than
    stopping at the first, matching §13's "không có nhánh lỗi riêng".
    """
    draft = spec()

    result = submit_for_approval(draft, form_confirmed=True, sandbox_run_count=1, deepeval_pass_rate=0.95)
    assert result.metadata.status is SpecStatus.PENDING_APPROVAL
    assert draft.metadata.status is SpecStatus.DRAFT  # the version passed in is untouched

    refused = refusal(
        submit_for_approval, draft, form_confirmed=False, sandbox_run_count=0, deepeval_pass_rate=0.0
    )
    assert {problem.code for problem in refused.problems} == {
        "form_not_confirmed",
        "sandbox_run_missing",
        "deepeval_below_threshold",
    }


def test_deepeval_threshold_reads_the_spec_own_evaluation_block() -> None:
    """FR-33: the bar is `evaluation.min_pass_rate`, not a constant — and falls back
    to `Evaluation`'s own default when the spec carries no `evaluation` block.

    Both halves are asserted against `Evaluation().min_pass_rate` rather than a
    literal, so moving the default is a one-line change in one place. The
    literals that used to be here made the *gate's own value* something two
    files had to agree on, which is the shape of thing that goes stale quietly.
    """
    strict = spec(evaluation={"min_pass_rate": 0.99})
    refused = refusal(submit_for_approval, strict, form_confirmed=True, sandbox_run_count=1, deepeval_pass_rate=0.95)
    assert [problem.code for problem in refused.problems] == ["deepeval_below_threshold"]

    default_bar = spec()  # no `evaluation` block at all
    bar = Evaluation().min_pass_rate
    refused_default = refusal(
        submit_for_approval, default_bar, form_confirmed=True, sandbox_run_count=1, deepeval_pass_rate=bar - 0.05
    )
    assert [problem.code for problem in refused_default.problems] == ["deepeval_below_threshold"]

    passes_default = submit_for_approval(
        default_bar, form_confirmed=True, sandbox_run_count=1, deepeval_pass_rate=bar
    )
    assert passes_default.metadata.status is SpecStatus.PENDING_APPROVAL


def test_approval_needs_the_spec_own_quorum_met() -> None:
    """HITL #2: `authority.approval_quorum` distinct signatures, not a fixed number."""
    pending = spec(
        authority={
            "autonomy": "propose",
            "approval_quorum": 2,
            "budget": {"max_tool_calls_per_session": 40},
        },
        metadata={**spec_payload()["metadata"], "status": "pending_approval"},
    )

    refused = refusal(approve, pending, approver_ids=["E101"])
    assert [problem.code for problem in refused.problems] == ["approval_quorum_not_met"]

    approved = approve(pending, approver_ids=["E101", "E102"])
    assert approved.metadata.status is SpecStatus.APPROVED
    assert approved.metadata.approved_by == ["E101", "E102"]


def test_duplicate_approver_ids_count_as_one_signature() -> None:
    """A quorum counts distinct people — the same id signing twice is not two signatures."""
    pending = spec(
        authority={
            "autonomy": "propose",
            "approval_quorum": 2,
            "budget": {"max_tool_calls_per_session": 40},
        },
        metadata={**spec_payload()["metadata"], "status": "pending_approval"},
    )

    refused = refusal(approve, pending, approver_ids=["E101", "E101", "E101"])
    assert [problem.code for problem in refused.problems] == ["approval_quorum_not_met"]

    approved = approve(pending, approver_ids=["E101", "E101", "E102"])
    assert approved.metadata.approved_by == ["E101", "E102"]


@pytest.mark.parametrize(
    ("fn", "kwargs", "required_status"),
    [
        (submit_for_approval, {"form_confirmed": True, "sandbox_run_count": 1, "deepeval_pass_rate": 1.0}, "draft"),
        (approve, {"approver_ids": ["E101", "E102", "E103", "E104", "E105"]}, "pending_approval"),
        (deploy, {}, "approved"),
        (suspend, {"reason": SuspensionReason.ADMIN_KILL_SWITCH}, "deployed"),
    ],
)
def test_each_move_is_refused_from_every_status_but_its_own(fn, kwargs, required_status) -> None:
    """AC 1 generalised: a move only fires from the one status the table names for it."""
    for status in SpecStatus:
        if status.value == required_status:
            continue
        wrong = spec(metadata={**spec_payload()["metadata"], "status": status.value})
        refused = refusal(fn, wrong, **kwargs)
        assert [problem.code for problem in refused.problems] == ["invalid_transition"]


def test_deploy_only_moves_the_status_and_leaves_provisioning_to_the_compiler() -> None:
    approved = spec(metadata={**spec_payload()["metadata"], "status": "approved"})
    deployed = deploy(approved)
    assert deployed.metadata.status is SpecStatus.DEPLOYED
    assert approved.metadata.status is SpecStatus.APPROVED  # untouched


def test_suspend_moves_deployed_to_suspended_regardless_of_reason() -> None:
    deployed = spec(metadata={**spec_payload()["metadata"], "status": "deployed"})

    for reason in SuspensionReason:
        result = suspend(deployed, reason=reason)
        assert result.metadata.status is SpecStatus.SUSPENDED


def test_rollback_moves_approved_or_suspended_to_deployed_without_a_new_version() -> None:
    """FR-51/AC: "rollback đổi đúng con trỏ, không tạo version mới, không cần duyệt lại"."""
    for starting_status in ("approved", "suspended"):
        target = spec(metadata={**spec_payload()["metadata"], "version": 2, "status": starting_status})
        rolled_back = rollback(target)
        assert rolled_back.metadata.status is SpecStatus.DEPLOYED
        assert rolled_back.metadata.version == 2  # no new version allocated
        assert target.metadata.status.value == starting_status  # untouched


def test_rollback_is_refused_from_every_other_status() -> None:
    for status in SpecStatus:
        if status in (SpecStatus.APPROVED, SpecStatus.SUSPENDED):
            continue
        wrong = spec(metadata={**spec_payload()["metadata"], "status": status.value})
        refused = refusal(rollback, wrong)
        assert [problem.code for problem in refused.problems] == ["invalid_transition"]


def test_editing_a_deployed_spec_always_creates_a_new_draft_version_never_in_place() -> None:
    """AC 2 — the only edit path is `new_draft_version`; there is no way to mutate a stored version."""
    deployed = spec(metadata={**spec_payload()["metadata"], "version": 3, "status": "deployed"})

    new_version = new_draft_version(deployed)

    assert new_version.metadata.version == 4
    assert new_version.metadata.status is SpecStatus.DRAFT
    assert new_version.metadata.parent_version == 3
    assert new_version.metadata.approved_by == []
    # the version this was called on is untouched — proves "never in place"
    assert deployed.metadata.version == 3
    assert deployed.metadata.status is SpecStatus.DEPLOYED


def test_new_draft_version_is_available_from_every_status_including_retired() -> None:
    """§13's wildcard reads "any -> draft (v+1)" with no stated exception, retired included."""
    for status in SpecStatus:
        version = spec(metadata={**spec_payload()["metadata"], "version": 1, "status": status.value})
        new_version = new_draft_version(version)
        assert new_version.metadata.status is SpecStatus.DRAFT
        assert new_version.metadata.version == 2
