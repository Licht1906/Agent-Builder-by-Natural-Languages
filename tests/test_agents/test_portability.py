"""Carrying an agent out as JSON and back in — `src.agents.portability`.

Two properties are worth pinning here and the rest follows from them.

The first is that a bundle is the stored documents and not a rendering of them,
so a round trip changes nothing. The moment export starts reshaping a document,
an import stops producing the agent that was exported and nothing downstream
says so.

The second is what an import is *allowed* to change, which is exactly one thing:
a node model this deployment's admin does not allow. That is the one field a
creator cannot fix on the canvas — it is not their decision — so leaving it would
let an import name a model the platform's own composition step would never write
(ADR-0010 §4). Everything else that only makes sense where the file came from
travels verbatim and is left for the canvas to ring red.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.orm import Session

from src.admin.config import read_config, update_config
from src.agents.portability import BUNDLE_FORMAT, BundleError, export_bundle, import_bundle
from src.db.models import Agent, AgentVersion, User, VersionStatus
from src.models.solution_spec import AgentSolutionSpecification
from src.models.workflow import WorkflowJSON

SPEC: dict[str, Any] = {
    "goal": "Trả lời khiếu nại của khách",
    "users": ["Nhân viên chăm sóc khách hàng"],
    "data": [{"name": "Sổ tay", "kind": "uploaded_file", "ref": "so-tay.pdf"}],
    "actions": ["Soạn thư trả lời"],
    "expected_outcome": ["Một thư trả lời chờ duyệt"],
    "success_criteria": [{"text": "Bám theo sổ tay", "category": "source_groundedness"}],
    "prohibitions": ["Không tự gửi thư"],
}

GRAPH: dict[str, Any] = {
    "start_node_id": "answer",
    "nodes": [
        {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời", "model": "gpt-4.1"}},
        {"id": "tra_cuu", "type": "knowledge_search", "config": {"source_ids": ["so-tay.pdf"]}},
        {"id": "done", "type": "end", "config": {}},
    ],
    "edges": [{"id": "e1", "source_node_id": "answer", "source_port": "done", "target_node_id": "done"}],
    "metadata": {},
}


@pytest.fixture
def restored_allowlist(db_session: Session) -> Iterator[None]:
    """Put the platform's model allowlist back after a test has narrowed it.

    `system_config` is one row on a schema built once per session, so a test that
    changes the allowlist changes it for every test that runs afterwards — which
    is a failure that lands on somebody else's assertion, in another file, in an
    order that depends on how the suite was invoked.
    """
    before = list(read_config(db_session).model_allowlist)
    yield
    update_config(db_session, model_allowlist=before)


@pytest.fixture
def exported(db_session: Session, seeded_creator: User, solution_spec_version: Any) -> tuple[Agent, AgentVersion]:
    """One approved version of one agent — something worth exporting."""
    version = solution_spec_version(seeded_creator, SPEC, status=VersionStatus.APPROVED, workflow=GRAPH)
    return db_session.get(Agent, version.agent_id), version  # type: ignore[return-value]


def test_a_bundle_is_the_stored_documents_and_survives_a_round_trip(
    db_session: Session, seeded_creator: User, exported: tuple[Agent, AgentVersion]
) -> None:
    """Export, import, export again — and the second file matches the first.

    The strongest thing that can be said about a portable format, and the reason
    both halves are `model_validate`/`model_dump` on the *same* models rather
    than bespoke code at either end.
    """
    agent, version = exported

    bundle = export_bundle(agent, version)
    assert bundle["format"] == BUNDLE_FORMAT
    assert bundle["name"] == agent.name
    assert bundle["source_version_id"] == version.id

    imported = import_bundle(
        db_session,
        owner_id=seeded_creator.id,
        name="Bản nhập lại",
        spec=AgentSolutionSpecification.model_validate(bundle["solution_spec"]),
        workflow=WorkflowJSON.model_validate(bundle["workflow"]),
    )
    again = export_bundle(imported.agent, imported.version)

    assert again["solution_spec"] == bundle["solution_spec"]
    assert again["workflow"] == bundle["workflow"]


def test_an_import_lands_editable_rather_than_confirmed(
    db_session: Session, seeded_creator: User, exported: tuple[Agent, AgentVersion]
) -> None:
    """`workflow_pending_confirmation`, on a brand-new agent.

    An imported graph is one this deployment has never validated. Landing it
    confirmed would be the one agent on the platform that skipped the gate every
    other agent passes; landing it editable is what puts its unresolved
    references on the canvas as problems to fix.
    """
    agent, version = exported
    bundle = export_bundle(agent, version)

    imported = import_bundle(
        db_session,
        owner_id=seeded_creator.id,
        name="Bản nhập lại",
        spec=AgentSolutionSpecification.model_validate(bundle["solution_spec"]),
        workflow=WorkflowJSON.model_validate(bundle["workflow"]),
    )

    assert imported.agent.id != agent.id
    assert imported.agent.owner_id == seeded_creator.id
    assert imported.version.status is VersionStatus.WORKFLOW_PENDING_CONFIRMATION
    assert imported.version.solution_spec == version.solution_spec
    assert imported.remapped_models == {}


def test_references_that_only_exist_where_the_file_came_from_travel_verbatim(
    db_session: Session, seeded_creator: User, exported: tuple[Agent, AgentVersion]
) -> None:
    """`source_ids` is imported unchanged, not stripped.

    The document it names belongs to the exporter's library and may well not
    exist here. Dropping it would produce a graph that confirms cleanly and then
    answers from nothing; keeping it produces one the validator refuses by name
    until the creator repoints it, which is the failure that can be acted on.
    """
    agent, version = exported
    bundle = export_bundle(agent, version)

    imported = import_bundle(
        db_session,
        owner_id=seeded_creator.id,
        name="Bản nhập lại",
        spec=AgentSolutionSpecification.model_validate(bundle["solution_spec"]),
        workflow=WorkflowJSON.model_validate(bundle["workflow"]),
    )

    node = WorkflowJSON.model_validate(imported.version.workflow).node("tra_cuu")
    assert node is not None
    assert node.config["source_ids"] == ["so-tay.pdf"]


@pytest.mark.usefixtures("restored_allowlist")
def test_a_model_this_deployment_does_not_allow_is_reassigned_and_reported(
    db_session: Session, seeded_creator: User, exported: tuple[Agent, AgentVersion]
) -> None:
    """The one thing an import rewrites, and it says so per node.

    A creator not told that a step changed model has been handed a different
    agent than the one in the file — so the remap is reported by node id, which
    is what they need to go and look at.
    """
    agent, version = exported
    bundle = export_bundle(agent, version)
    # The bundle names `gpt-4.1`; this platform now allows only `gpt-4.1-mini`.
    update_config(db_session, model_allowlist=["gpt-4.1-mini"])

    imported = import_bundle(
        db_session,
        owner_id=seeded_creator.id,
        name="Bản nhập lại",
        spec=AgentSolutionSpecification.model_validate(bundle["solution_spec"]),
        workflow=WorkflowJSON.model_validate(bundle["workflow"]),
    )

    assert imported.remapped_models == {"answer": ("gpt-4.1", "gpt-4.1-mini")}
    node = WorkflowJSON.model_validate(imported.version.workflow).node("answer")
    assert node is not None
    assert node.config["model"] == "gpt-4.1-mini"


def test_a_version_missing_a_document_refuses_rather_than_exporting_half_a_bundle(
    db_session: Session, seeded_creator: User, solution_spec_version: Any
) -> None:
    """A file with a null workflow would import into an agent with nothing to run."""
    version = solution_spec_version(seeded_creator, SPEC, status=VersionStatus.SPEC_CONFIRMED, workflow=None)
    agent = db_session.get(Agent, version.agent_id)

    with pytest.raises(BundleError, match="chưa đủ tài liệu"):
        export_bundle(agent, version)  # type: ignore[arg-type]
