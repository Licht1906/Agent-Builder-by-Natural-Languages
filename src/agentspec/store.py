"""UC1-38 (#126) — persisting an `AgentSpec` by `(agent_id, version)`, the
first thing that needs to address one over HTTP rather than pass it around as
a value.

Deliberately thin: this is not the full version-lifecycle CRUD surface
(`POST /agents`, `PATCH .../versions/{v}`, `.../confirm` — spec.md §19)
those routes are UC1-04/UC1-24's territory (E2/E10) and unassigned to this
batch of tickets. What sandbox chat actually needs is *somewhere* to read a
spec back from by id — `save_spec_version`/`get_spec_version` are exactly
that and nothing more. `get_deployed_spec_version` is UC1-18 (#134)'s own
addition: the one extra read `POST /agents/{id}/suspend` needs, still a read
by a status this table already carries rather than a second CRUD surface.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agentspec.spec import AgentSpec, SpecStatus
from src.db.models import UC1AgentSpecVersion


class SpecVersionNotFoundError(LookupError):
    """No row named `(agent_id, version)` — `str(self)` names both."""

    def __init__(self, agent_id: str, version: int) -> None:
        self.agent_id = agent_id
        self.version = version
        super().__init__(f"Không tìm thấy agent {agent_id!r} version {version}")


def save_spec_version(db: Session, spec: AgentSpec, *, created_by_id: int) -> UC1AgentSpecVersion:
    """Persist `spec` under its own `(metadata.id, metadata.version)`.

    Takes an already-validated `AgentSpec`, never a raw payload — the one
    gate (`src.agentspec.validation.validate_spec`) has already run by the
    time anything reaches here, the same discipline `src.agentspec.lifecycle`
    keeps.
    """
    row = UC1AgentSpecVersion(
        agent_id=spec.metadata.id,
        version=spec.metadata.version,
        spec=spec.model_dump(mode="json", by_alias=True),
        created_by_id=created_by_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_spec_version_status(db: Session, spec: AgentSpec) -> UC1AgentSpecVersion:
    """Persist a lifecycle move (`src.agentspec.lifecycle`) on `(agent_id, version)`, in place.

    Unlike `save_spec_version`, this overwrites the existing row rather than
    inserting a new one: a lifecycle move (`draft -> pending_approval`, ...)
    is not `new_draft_version`'s "any -> draft (v+1)" — it stays on the same
    version and updates the one thing the move actually changed
    (`metadata.status`, and for `approve`, `metadata.approved_by`).
    """
    row = db.execute(
        select(UC1AgentSpecVersion).where(
            UC1AgentSpecVersion.agent_id == spec.metadata.id, UC1AgentSpecVersion.version == spec.metadata.version
        )
    ).scalar_one_or_none()
    if row is None:
        raise SpecVersionNotFoundError(spec.metadata.id, spec.metadata.version)
    row.spec = spec.model_dump(mode="json", by_alias=True)
    db.commit()
    db.refresh(row)
    return row


def get_spec_version(db: Session, agent_id: str, version: int) -> AgentSpec:
    """The `AgentSpec` last saved under `(agent_id, version)`, or `SpecVersionNotFoundError`."""
    row = db.execute(
        select(UC1AgentSpecVersion).where(
            UC1AgentSpecVersion.agent_id == agent_id, UC1AgentSpecVersion.version == version
        )
    ).scalar_one_or_none()
    if row is None:
        raise SpecVersionNotFoundError(agent_id, version)
    return AgentSpec.model_validate(row.spec)


def get_deployed_spec_version(db: Session, agent_id: str) -> AgentSpec | None:
    """The one version of `agent_id` currently at `metadata.status == deployed`, or `None`.

    UC1-18 (#134)'s `POST /agents/{id}/suspend` targets the agent, not a
    version — the kill switch is "stop whatever is live", and the version
    that is live is a fact about `status`, not something a caller names. Reads
    the JSON column's own `metadata.status` (`->>`) rather than loading every
    version and re-validating each with Pydantic just to check one field —
    the same reasoning `src.approvals`'s queue query gives for filtering in
    SQL wherever the filter is on a value the row already carries.
    """
    row = db.execute(
        select(UC1AgentSpecVersion).where(
            UC1AgentSpecVersion.agent_id == agent_id,
            UC1AgentSpecVersion.spec.op("->")("metadata").op("->>")("status") == SpecStatus.DEPLOYED.value,
        )
    ).scalar_one_or_none()
    return AgentSpec.model_validate(row.spec) if row is not None else None


def list_deployed_specs(db: Session) -> list[AgentSpec]:
    """Every agent's currently-`deployed` version, ordered by `agent_id`.

    `get_deployed_spec_version` above answers "what is live for *this* agent";
    this answers "what is live at all", which is the question an end-user
    catalog asks — nobody outside the admin surface knows an `agent_id` to look
    one up by, and until this existed there was no way to find out.

    Filters on `metadata.status` in SQL for the same reason its single-agent
    sibling does, and returns validated `AgentSpec`s rather than rows because
    every caller needs `audience` off the document to decide who may see it.
    """
    rows = (
        db.execute(
            select(UC1AgentSpecVersion)
            .where(UC1AgentSpecVersion.spec.op("->")("metadata").op("->>")("status") == SpecStatus.DEPLOYED.value)
            .order_by(UC1AgentSpecVersion.agent_id)
        )
        .scalars()
        .all()
    )
    return [AgentSpec.model_validate(row.spec) for row in rows]
