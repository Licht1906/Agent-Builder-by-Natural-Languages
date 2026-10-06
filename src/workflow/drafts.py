"""Save, confirm, fork — the three writes the Workflow Canvas performs, #44.

The Generator (`src.workflow.generator`) puts a graph on a version and stops.
Everything that happens to that graph afterwards happens here, and it is a state
machine with exactly two states and one door out of each:

    workflow_pending_confirmation --save--> workflow_pending_confirmation
    workflow_pending_confirmation --confirm--> workflow_confirmed
    <any WORKFLOW_FORKABLE stage> --save--> a *new* version at
                                            workflow_pending_confirmation

The third line is the whole of the immutability promise, and its left-hand side
is `src.agents.versions.WORKFLOW_FORKABLE` rather than a status named here — see
that constant for why it is every stage after confirmation and not just the
first. What matters at this end is that the fork is the *only* thing an edit to
a frozen graph can become: widening the door widened where an edit may arrive
from, and changed nothing about where it lands.

**Saving never validates the graph, and confirming always does.** That asymmetry
is ADR-0022 §6, and it is the reason the two are separate functions rather than
one `save(confirm=True)`: a creator halfway through wiring a branch has a
document no rule accepts, and a save that ran the validator would refuse to
store the very state editing passes through. `graph_problems` is what the canvas
shows them in the meantime — reported, not enforced.

**Confirming is the only way to `workflow_confirmed`, and `save_workflow` is the
only writer of `AgentVersion.workflow` after the Generator.** Both halves matter.
The first makes `ensure_confirmable` unavoidable rather than a check a future
route might forget; the second is what lets "confirmed means immutable" be a
property of the code instead of a convention — there is no second door, so an
edit arriving at a confirmed version has nowhere to go but a fork.

Shaped after `src.solution.specs`, which gates the document one stage earlier
(#42). Separate module, separate exception types, same skeleton: a stage gate, a
document written to `AgentVersion`, and no other write path in.
"""

from sqlalchemy.orm import Session

from src.agents.versions import WORKFLOW_FORKABLE, wrong_stage
from src.db.models import AgentVersion, VersionStatus
from src.models.workflow import WorkflowJSON
from src.workflow.generator import WorkflowStageError, workflow_of
from src.workflow.knowledge_bases import ensure_knowledge_bases_owned
from src.workflow.validation import ensure_confirmable


def save_workflow(db: Session, version: AgentVersion, workflow: WorkflowJSON) -> AgentVersion:
    """Store `workflow` as `version`'s graph, however unfinished it is.

    The canvas's ordinary save (user story #15). Nothing here asks whether the
    graph is any good — a node with a blank prompt, a port wired to nothing and a
    branch that never reaches `End` all store exactly as they arrived, because
    each of them is a state a creator passes through on the way to a graph that
    has none of those things.

    **Returns the version the document ended up on, which is not always the one
    passed in.** Editing a `workflow_confirmed` version forks (`_fork_for_edit`),
    so a caller must read the id off the result rather than assume it still has
    the row it started with. That is the whole of ADR-0022 §5's immutability
    promise expressed as a return value: there is no argument to this function
    that overwrites a confirmed graph, and no other function that writes the
    column at all.

    Refused at every other status. A version at `spec_confirmed` has no graph for
    a canvas to be editing, and a `draft` is V1's document, not this one.
    """
    if version.status in WORKFLOW_FORKABLE:
        if version.workflow is None:
            # `sandbox_passed` is shared with V1, whose versions carry a `spec`
            # and no graph (`AWAITING_SANDBOX`). Forking one would mint a version
            # holding a workflow that answers no confirmed specification — the
            # thing `_fork_for_edit` carries `solution_spec` across to prevent.
            raise WorkflowStageError(
                f"Không thể lưu workflow: version {version.id} chưa có workflow nào để sửa"
            )
        return _fork_for_edit(db, version, workflow)
    _require_status(version, VersionStatus.WORKFLOW_PENDING_CONFIRMATION, action="lưu workflow")
    version.workflow = workflow.model_dump(mode="json")
    return _persist(db, version)


def confirm_workflow(db: Session, version: AgentVersion) -> AgentVersion:
    """`workflow_pending_confirmation -> workflow_confirmed`, behind two graph gates.

    User story #16, and the one transition `src.workflow.validation` and
    `src.workflow.knowledge_bases` both exist in front of. `ensure_confirmable`
    checks the graph's own shape — nodes, edges, blanks, guarded side effects —
    and needs no database to answer. `ensure_knowledge_bases_owned` (ADR-0043
    §1, RAG-05 #91) checks something the graph's shape alone cannot: whether
    every `KnowledgeSearch` node's `source_ids` names a Knowledge Base that
    still exists and belongs to this version's own agent's owner, not a shelf
    a stored document merely claims. Both raise `WorkflowGraphError` naming
    every problem at once — deliberately not caught here, because the sentence
    each writes is the one the creator has to read, and re-wrapping it would
    replace fixable problems with the news that there were some.

    The stage check comes **before** either graph check, and the order is the
    difference between two true sentences. A version already confirmed is not
    "a graph with problems"; it is a graph somebody else already agreed to, and
    saying so is what stops a second caller believing it confirmed what it read.

    A refusal from any of the three checks leaves the document untouched:
    nothing is written until all of them have passed.
    """
    _require_status(version, VersionStatus.WORKFLOW_PENDING_CONFIRMATION, action="xác nhận workflow")
    workflow = workflow_of(version)
    ensure_confirmable(workflow)
    ensure_knowledge_bases_owned(db, workflow, version.agent.owner_id)
    version.status = VersionStatus.WORKFLOW_CONFIRMED
    return _persist(db, version)


def _fork_for_edit(db: Session, version: AgentVersion, workflow: WorkflowJSON) -> AgentVersion:
    """A new version of the same agent, holding `workflow` and waiting to be confirmed.

    What an edit to a confirmed graph becomes (ADR-0022 §5). The same rule V1
    drew around a deployed spec, one stage earlier: behaviour changes by adding a
    version, never by rewriting one somebody has already signed off.

    The confirmed `solution_spec` is carried across rather than left behind. It
    is the business problem both graphs answer — unchanged by anything happening
    on the canvas — and a version holding a workflow with no spec behind it would
    be a graph nobody can state the purpose of, which is also a version the
    Generator's own reader (`solution_spec_of`) refuses by name.

    `spec` is deliberately **not** carried: V1's executable pipeline is a
    different document, produced further down the line, and copying one across a
    fork would attach a pipeline to a graph that has not been confirmed yet.
    """
    forked = AgentVersion(
        agent_id=version.agent_id,
        solution_spec=version.solution_spec,
        workflow=workflow.model_dump(mode="json"),
        status=VersionStatus.WORKFLOW_PENDING_CONFIRMATION,
    )
    db.add(forked)
    return _persist(db, forked)


def _require_status(version: AgentVersion, expected: VersionStatus, *, action: str) -> None:
    """Refuse unless `version` is at the one stage where `action` is allowed.

    The sentence comes from `wrong_stage`, shared with the spec's gate (#42) and
    the Generator's (#43); the exception type is the Generator's on purpose, not
    a fourth one — a caller handling "something about this version's workflow is
    at the wrong stage" should not have to catch two types to hear it.
    """
    refusal = wrong_stage(version, expected, action=action)
    if refusal is not None:
        raise WorkflowStageError(refusal)


def _persist(db: Session, version: AgentVersion) -> AgentVersion:
    """Commit and refresh — the tail every mutating function here ends with."""
    db.commit()
    db.refresh(version)
    return version
