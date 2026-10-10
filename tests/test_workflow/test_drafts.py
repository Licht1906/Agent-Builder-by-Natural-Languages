"""The two transitions the Workflow Canvas performs — #44, ADR-0022 §5–6.

Saving and confirming pull in opposite directions on purpose, and both directions
are asserted here. A save takes whatever the creator has on the canvas, however
half-finished, because a creator who cannot store an unfinished graph cannot
finish one. A confirmation takes nothing that the runtime could not honestly
execute, because it is the moment the document stops being editable.

The third transition is the consequence of the second: once a graph is confirmed
an edit has nowhere to go, so it forks. These tests are what pin "confirmed means
immutable" to something other than a promise in a docstring — they check the
original row is still exactly as it was after somebody edited it.
"""

from typing import Any

import pytest
from sqlalchemy.orm import Session

from src.db.models import AgentVersion, User, VersionStatus
from src.models.workflow import WorkflowJSON
from src.workflow.drafts import confirm_workflow, save_workflow
from src.workflow.generator import WorkflowStageError, workflow_of

SPEC: dict[str, Any] = {
    "goal": "Trả lời khiếu nại của khách",
    "users": ["Nhân viên chăm sóc khách hàng"],
    "data": [{"name": "Sổ tay", "kind": "uploaded_file", "ref": "blob://so-tay.pdf"}],
    "actions": ["Tra cứu đơn hàng"],
    "expected_outcome": ["Một thư trả lời"],
    "success_criteria": [{"text": "Nêu đúng mã đơn", "category": "correctness"}],
    "prohibitions": ["Không hứa hoàn tiền"],
}


def graph(**overrides: Any) -> WorkflowJSON:
    """A workflow that passes every rule in `ensure_confirmable`, plus `overrides`."""
    document: dict[str, Any] = {
        "start_node_id": "answer",
        "nodes": [
            {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời", "model": "gpt-4o-mini"}},
            {"id": "done", "type": "end", "config": {}},
        ],
        "edges": [{"id": "e1", "source_node_id": "answer", "source_port": "done", "target_node_id": "done"}],
        "metadata": {},
    }
    return WorkflowJSON.model_validate(document | overrides)


@pytest.fixture
def drafting(solution_spec_version: Any, seeded_creator: User) -> Any:
    """A version at `workflow_pending_confirmation` holding `document`."""

    def _make(
        document: WorkflowJSON | None = None,
        *,
        status: VersionStatus = VersionStatus.WORKFLOW_PENDING_CONFIRMATION,
    ) -> AgentVersion:
        return solution_spec_version(
            seeded_creator,
            SPEC,
            status=status,
            workflow=(document or graph()).model_dump(mode="json"),
        )

    return _make


def test_confirming_a_valid_graph_makes_it_immutable(db_session: Session, drafting: Any) -> None:
    """AC 5, first half — the transition the whole ticket is a gate in front of."""
    version = drafting()

    confirmed = confirm_workflow(db_session, version)

    assert confirmed.status is VersionStatus.WORKFLOW_CONFIRMED


def test_editing_a_confirmed_workflow_forks_a_new_version(db_session: Session, drafting: Any) -> None:
    """AC 5, second half — an edit after confirmation has nowhere to go but a new row."""
    version = drafting()
    confirm_workflow(db_session, version)
    edited = graph(
        nodes=[
            {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời thật kỹ", "model": "gpt-4o"}},
            {"id": "done", "type": "end", "config": {}},
        ]
    )

    forked = save_workflow(db_session, version, edited)

    assert forked.id != version.id
    assert forked.agent_id == version.agent_id
    assert forked.status is VersionStatus.WORKFLOW_PENDING_CONFIRMATION
    assert workflow_of(forked).node("answer").config["model"] == "gpt-4o"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "status",
    [
        VersionStatus.SANDBOX_PASSED,
        VersionStatus.PENDING_APPROVAL,
        VersionStatus.APPROVED,
        VersionStatus.REJECTED,
    ],
)
def test_every_frozen_stage_forks_rather_than_dead_ending(
    db_session: Session, drafting: Any, status: VersionStatus
) -> None:
    """`WORKFLOW_FORKABLE`, asserted stage by stage — and the original left alone.

    Each of these used to refuse, and each refusal was a trap rather than a
    decision. `sandbox_passed` meant running your workflow once cost you the
    ability to change it. `rejected` meant an admin could ask for a change the
    creator had no way to make. `approved` meant a published agent could never be
    improved.

    The original row is re-read at the end rather than trusted, because "editing
    a live version does not touch what is serving" is the promise the deployed
    pointer rests on (ADR-0029 §1), and a fork that mutated its parent would
    break it silently.
    """
    version = drafting(status=status)
    before = workflow_of(version).model_dump(mode="json")
    edited = graph(
        nodes=[
            {"id": "answer", "type": "agent_llm", "config": {"prompt": "Sửa sau khi đã chốt", "model": "gpt-4o"}},
            {"id": "done", "type": "end", "config": {}},
        ]
    )

    forked = save_workflow(db_session, version, edited)

    assert forked.id != version.id
    assert forked.agent_id == version.agent_id
    assert forked.status is VersionStatus.WORKFLOW_PENDING_CONFIRMATION
    assert forked.solution_spec == version.solution_spec

    db_session.refresh(version)
    assert version.status is status
    assert workflow_of(version).model_dump(mode="json") == before


def test_a_version_with_no_graph_refuses_the_edit_rather_than_forking_an_empty_one(
    db_session: Session, seeded_creator: User, solution_spec_version: Any
) -> None:
    """A V1 version at `sandbox_passed` shares the stage but holds no workflow.

    `sandbox_passed` is reachable from both pipelines (`AWAITING_SANDBOX`), so
    membership of `WORKFLOW_FORKABLE` is not on its own enough to fork on: a V1
    row has a `spec` and a null `workflow`, and forking it would mint a version
    whose graph answers a specification it never had.
    """
    version = solution_spec_version(seeded_creator, SPEC, status=VersionStatus.SANDBOX_PASSED, workflow=None)

    with pytest.raises(WorkflowStageError, match="chưa có workflow"):
        save_workflow(db_session, version, graph())
