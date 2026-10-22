"""Knowledge Bases, and which Agents may search them — #90 / ADR-0043.

A Knowledge Base is the shelf ADR-0043 §1 argues a `KnowledgeSearch` node
should address instead of a single Document: a named, mutable collection a
creator can add to without that being a change to the workflow that names it.
This module owns the shelf itself — creating one, listing an owner's own,
deleting one, and saying which Agents may search it — and nothing about what
sits on it, which is `src.workflow.documents`'s job, or about how a node
actually searches one at run time, which stays the toy term-overlap reader
(`src.workflow.knowledge`) until RAG-10 (#96) swaps in a real retriever.

What this module *does* own since RAG-05 (#91): the confirm-time question of
whether a `KnowledgeSearch` node's `source_ids` are honest. `source_ids` now
holds Knowledge Base ids rather than file names (ADR-0043 §1), and an id in a
stored graph is just a string a client sent — it could name a shelf that was
since deleted, or, since a `WorkflowJSON` carries no owner of its own, a shelf
belonging to a creator entirely different from the one confirming this graph.
`knowledge_base_problems`/`ensure_knowledge_bases_owned` are that check, run
next to `src.workflow.validation.ensure_confirmable` rather than folded into
it, because answering it needs a database session and `ensure_confirmable` is
also the pure, DB-free check `ensure_runnable` re-runs in front of every run.

Like `src.deployments.publication`, this has no HTTP surface of its own: every
function takes and returns ORM rows, and `src.api.knowledge_base_routes` is
the only thing that turns them into responses or 404s.
"""

from collections.abc import Sequence
from typing import TypeVar

from pydantic import ValidationError
from qdrant_client import QdrantClient
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from src.db.models import Agent, AgentVersion, ExternalConnection, KnowledgeBase, KnowledgeBaseMode, VersionStatus
from src.models.node_registry import KnowledgeSearchConfig, NodeType
from src.models.workflow import WorkflowJSON
from src.services.qdrant import QdrantWriteError, delete_knowledge_base_points, get_qdrant_client
from src.workflow.validation import GraphProblem, WorkflowGraphError

_Row = TypeVar("_Row")


def create_knowledge_base(
    db: Session,
    owner_id: int,
    name: str,
    description: str = "",
    *,
    external_connection: ExternalConnection | None = None,
) -> KnowledgeBase:
    """A fresh shelf belonging to `owner_id` — empty, or pointed at a database this platform does not run.

    `external_connection` is what decides `mode` (RAG-22 / #108), rather than
    the two being passed in separately and trusted to agree: a `byodb` row
    with nothing to search and a `managed` row naming a database are both
    states no caller should be able to write, and the way to make them
    unreachable is to derive one field from the other here instead of
    checking them against each other in every caller.

    The connection is taken as a row, not an id, so ownership is already
    settled before this is called — `src.api.knowledge_base_routes` resolves
    it through the same 404-not-403 dependency every other cross-resource
    reference goes through, and this module never re-decides who may see what.
    """
    knowledge_base = KnowledgeBase(
        owner_id=owner_id,
        name=name,
        description=description,
        mode=KnowledgeBaseMode.BYODB if external_connection is not None else KnowledgeBaseMode.MANAGED,
        external_connection_id=external_connection.id if external_connection is not None else None,
    )
    db.add(knowledge_base)
    return _persist(db, knowledge_base)


def update_knowledge_base(
    db: Session, knowledge_base: KnowledgeBase, *, name: str | None = None, description: str | None = None
) -> KnowledgeBase:
    """Rename `knowledge_base` or change its description — whichever of the two is given.

    `None` means "leave this one alone" rather than "clear it", so a caller
    renaming a shelf does not have to resend the description it already had.
    """
    if name is not None:
        knowledge_base.name = name
    if description is not None:
        knowledge_base.description = description
    return _persist(db, knowledge_base)


#: `document_count`/`agent_names` (RAG-13 / #99) each read a relationship —
#: without this, `KnowledgeBaseResponse.model_validate` on a *list* of shelves
#: is 1 + 2N queries, one pair of lazy loads per row the list screen shows.
#: `selectinload` batches each relationship into one extra query for the whole
#: page instead, the same fix `list_running_agents` uses for its own owner
#: join (`src.api.agent_routes`) rather than a `.owner_email` computed per row.
_WITH_LIST_SCREEN_FIELDS = (selectinload(KnowledgeBase.documents), selectinload(KnowledgeBase.agents))


def list_knowledge_bases(db: Session, owner_id: int) -> Sequence[KnowledgeBase]:
    """Every shelf `owner_id` owns, newest first."""
    return (
        db.execute(
            select(KnowledgeBase)
            .where(KnowledgeBase.owner_id == owner_id)
            .order_by(KnowledgeBase.id.desc())
            .options(*_WITH_LIST_SCREEN_FIELDS)
        )
        .scalars()
        .all()
    )


class KnowledgeBaseDeleteRefusedError(ValueError):
    """Deleting a Knowledge Base was refused. The message says why, and `agents` names who — structured, not just prose.

    RAG-13's confirmation screen has to list the affected agents by name
    before a caller decides whether to pass `confirm=True` — parsing them back
    out of the human-readable message would make the frontend a second parser
    for a sentence this module already knows the parts of.
    """

    def __init__(self, message: str, *, agents: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.agents = list(agents)


class KnowledgeBaseInUseError(KnowledgeBaseDeleteRefusedError):
    """A confirmed graph still points at this Knowledge Base — blocked outright, no `confirm` overrides it.

    A confirmed graph is immutable (ADR-0022 §5): there is no "unpoint it
    from this shelf first" move available even in principle, because editing
    a confirmed `WorkflowJSON` always forks a new `AgentVersion` rather than
    changing this one. Deleting the shelf out from under it would leave a
    `source_ids` entry a confirmed graph promised was real pointing at
    nothing — this is refused, not warned about.
    """


class KnowledgeBaseLinkedError(KnowledgeBaseDeleteRefusedError):
    """≥1 Agent currently links to this Knowledge Base (`agent_kb_links`) — pass `confirm=True` to delete anyway.

    Unlike a confirmed graph's `source_ids`, this link is an editable,
    present-tense assignment (`set_agent_knowledge_bases`) — an agent whose
    *unconfirmed* draft still lists this shelf loses nothing a person cannot
    just re-point. So this is a warning naming who is affected, not a block.
    """


#: Every stage an `AgentVersion` reaches only by having passed through
#: `workflow_confirmed` first (ADR-0022 §5's own list, minus the three
#: pre-confirmation stages) — a version at any of these has an immutable
#: `workflow` document, whatever `source_ids` it names still binding.
#:
#: `REJECTED` is included deliberately, not by omission: a rejected version
#: never runs again, but its `workflow` document is exactly as immutable as
#: an approved one's (ADR-0022 §5 does not carve out an exception for
#: versions an approval later declined), and this block exists because the
#: graph cannot be edited out of naming this shelf — not because the graph
#: might still execute. A hardcoded tuple rather than "every status after
#: `WORKFLOW_CONFIRMED`" so a future status inserted in the wrong place in
#: the enum cannot silently join or leave this set by accident.
_CONFIRMED_OR_LATER_STATUSES = (
    VersionStatus.WORKFLOW_CONFIRMED,
    VersionStatus.DRAFT,
    VersionStatus.SANDBOX_PASSED,
    VersionStatus.PENDING_APPROVAL,
    VersionStatus.APPROVED,
    VersionStatus.REJECTED,
)


def _confirmed_versions_referencing(db: Session, kb_id: int) -> list[tuple[Agent, AgentVersion]]:
    """Every `(Agent, AgentVersion)` whose confirmed-or-later `workflow` still names `kb_id`.

    Mirrors `knowledge_base_problems`'s node scan — the same "parse
    `KnowledgeSearchConfig`, skip what doesn't parse" logic, applied here to
    every candidate version in the database rather than to one in-memory
    `WorkflowJSON` a creator is about to confirm.
    """
    kb_id_str = str(kb_id)
    candidates = db.execute(
        select(AgentVersion, Agent)
        .join(Agent, Agent.id == AgentVersion.agent_id)
        .where(AgentVersion.status.in_(_CONFIRMED_OR_LATER_STATUSES))
        .where(AgentVersion.workflow.is_not(None))
    ).all()

    matches: list[tuple[Agent, AgentVersion]] = []
    for version, agent in candidates:
        try:
            workflow = WorkflowJSON.model_validate(version.workflow)
        except ValidationError:
            continue
        for node in workflow.nodes_of_type(NodeType.KNOWLEDGE_SEARCH):
            try:
                config = KnowledgeSearchConfig.model_validate(node.config)
            except ValidationError:
                continue
            if kb_id_str in config.source_ids:
                matches.append((agent, version))
                break
    return matches


def _ensure_no_confirmed_graph_references(db: Session, knowledge_base: KnowledgeBase) -> None:
    matches = _confirmed_versions_referencing(db, knowledge_base.id)
    if matches:
        agent_labels = [f"{agent.name} (version {version.id})" for agent, version in matches]
        raise KnowledgeBaseInUseError(
            f"Không thể xoá — graph đã confirm của {', '.join(agent_labels)} đang trỏ tới Knowledge Base này, "
            "và graph đã confirm không thể sửa (ADR-0022 §5)",
            agents=agent_labels,
        )


def _ensure_no_live_agent_links(knowledge_base: KnowledgeBase) -> None:
    linked_agents = list(knowledge_base.agents)
    if linked_agents:
        names = [agent.name for agent in linked_agents]
        raise KnowledgeBaseLinkedError(
            f"Knowledge Base này đang được gán cho: {', '.join(names)}. Gọi lại kèm xác nhận nếu vẫn muốn xoá.",
            agents=names,
        )


def delete_knowledge_base(
    db: Session, knowledge_base: KnowledgeBase, *, qdrant_client: QdrantClient | None = None, confirm: bool = False
) -> None:
    """Remove `knowledge_base` — its Documents and its Agent links go with it, by cascade.

    Two refusals come before anything is touched. First, unconditionally: a
    confirmed graph naming this shelf blocks the delete outright
    (`KnowledgeBaseInUseError`) — see that exception's docstring for why
    `confirm` cannot override it. Second, unless `confirm=True`: any Agent
    currently linked via `agent_kb_links` raises `KnowledgeBaseLinkedError`
    naming them, so a caller sees who is affected before choosing to proceed.

    **Qdrant first, Postgres commit last** (RAG-09 / ADR-0044 §1) — the same
    ordering `src.workflow.documents.delete_document` uses, for the same
    reason: a point that outlives its Postgres row is a multi-tenant leak
    that looks like a cited answer, and `db.delete(knowledge_base)` alone
    stages nothing a Qdrant failure would need to be undone.
    """
    _ensure_no_confirmed_graph_references(db, knowledge_base)
    if not confirm:
        _ensure_no_live_agent_links(knowledge_base)

    qdrant = qdrant_client if qdrant_client is not None else get_qdrant_client()
    try:
        delete_knowledge_base_points(qdrant, str(knowledge_base.id))
    except Exception as exc:  # noqa: BLE001 — any Qdrant failure must block the Postgres commit, not just some
        raise QdrantWriteError(
            f"Không xoá được dữ liệu Qdrant cho Knowledge Base {knowledge_base.name!r} — thử lại: {exc}"
        ) from exc

    db.delete(knowledge_base)
    db.commit()


def set_agent_knowledge_bases(db: Session, agent: Agent, knowledge_bases: Sequence[KnowledgeBase]) -> Agent:
    """Replace which shelves `agent` may search with exactly `knowledge_bases`.

    Whole-list replacement rather than an add/remove pair, for
    `src.deployments.publication.set_access`'s reason: the screen that calls
    this is showing the caller's complete current selection, and a partial
    write would make unassigning one shelf a different request from assigning
    another.
    """
    agent.knowledge_bases = list(knowledge_bases)
    return _persist(db, agent)


def _persist(db: Session, row: _Row) -> _Row:
    """Commit and refresh — the tail every mutating function here ends with (`src.deployments.publication`)."""
    db.commit()
    db.refresh(row)
    return row


def knowledge_base_problems(db: Session, workflow: WorkflowJSON, owner_id: int) -> tuple[GraphProblem, ...]:
    """Every `KnowledgeSearch` node in `workflow` whose `source_ids` names a shelf `owner_id` may not address.

    "May not address" covers two cases the same way, on purpose: an id with
    nothing behind it (deleted, or never real) and an id that names somebody
    else's shelf. Both are the same failure from a confirming creator's side —
    a source their agent cannot honestly search — and ADR-0043 §1's whole point
    is that this is a name resolved once, at confirm time, rather than a
    polymorphic reference resolved at run time with a validator guessing which
    of the two sentences to write.

    A node whose config does not even parse as `KnowledgeSearchConfig` is
    skipped rather than reported twice — `src.workflow.validation._node_configs`
    already names that malformed config, and a second sentence about the same
    node would only crowd out the first.
    """
    problems: list[GraphProblem] = []
    for node in workflow.nodes_of_type(NodeType.KNOWLEDGE_SEARCH):
        try:
            config = KnowledgeSearchConfig.model_validate(node.config)
        except ValidationError:
            continue
        for source_id in config.source_ids:
            if _owned_knowledge_base(db, source_id, owner_id) is None:
                problems.append(
                    GraphProblem(
                        f"Node {node.id!r} trỏ tới Knowledge Base {source_id!r} không tồn tại hoặc không thuộc về bạn",
                        node_id=node.id,
                    )
                )
    return tuple(problems)


def ensure_knowledge_bases_owned(db: Session, workflow: WorkflowJSON, owner_id: int) -> None:
    """Raise `WorkflowGraphError` unless every `source_ids` entry in `workflow` names a shelf `owner_id` owns.

    Called from `src.workflow.drafts.confirm_workflow`, next to
    `ensure_confirmable` rather than inside it — see this module's docstring
    for why the DB-dependent check has to live beside the pure one instead of
    folded into it. Same exception type and the same message prefix as
    `ensure_confirmable`, so both refusals read as one family of "workflow not
    ready" rather than two different errors a caller has to learn to tell
    apart.
    """
    problems = knowledge_base_problems(db, workflow, owner_id)
    if problems:
        raise WorkflowGraphError("Workflow chưa hợp lệ: " + "; ".join(problem.message for problem in problems))


def _owned_knowledge_base(db: Session, source_id: str, owner_id: int) -> KnowledgeBase | None:
    """The Knowledge Base `source_id` names, if it exists and belongs to `owner_id` — else `None`.

    `source_id` is a string wherever a node config holds it (`RESOURCE_REF`
    fields are uniformly `str`), but a `KnowledgeBase.id` is the table's
    integer primary key, so a `source_id` that is not even shaped like one is
    the same "no" as one that parses but names nothing.
    """
    try:
        knowledge_base_id = int(source_id)
    except ValueError:
        return None
    knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
    if knowledge_base is None or knowledge_base.owner_id != owner_id:
        return None
    return knowledge_base
