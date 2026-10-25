"""Carrying an agent out of this deployment as JSON, and back into one.

An agent's portable form is two documents and a name: the confirmed
`AgentSolutionSpecification` — the business problem — and the `WorkflowJSON`
that answers it. Both are already frozen, `extra="forbid"` Pydantic models, so
the exported file **is** the stored document rather than a rendering of it:
export is `model_dump(mode="json")` and import is `model_validate`. There is no
third description of either shape to keep in step, and a hand-edited file is
refused by the same schema that guards the API.

Nothing secret travels. A Deployment's API key lives on `Deployment` and never
on an `AgentVersion` (ADR-0029), so a bundle is safe to attach to a ticket.

**What travels but does not mean the same thing on the other side** is the
interesting part, and this module deliberately does not try to fix it:

- `knowledge_search.source_ids` name documents in the exporter's own library;
- `mcp.tool_id` names a connection an admin approved on *that* deployment;
- a node's `model` may be outside the importing platform's allowlist.

The first two are imported verbatim and left to ring red on the canvas. The
graph validator already reports exactly those nodes by name and the confirm gate
already refuses until they are repointed (`src.workflow.validation`), so the
honest thing is to let the screen that exists do the job — silently dropping a
`source_ids` would produce a graph that confirms and then answers from nothing.

The third is different, and is the one thing rewritten on the way in. A model
outside the allowlist is not something the creator can fix on the canvas — the
admin withdrew it — and leaving it would let an import name a model this
platform's own composition step would never have written (ADR-0010 §4). So it is
remapped by exactly the rule composition uses, and every remap is reported back
so the creator reads about it rather than discovering it in a bill.
"""

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from src.admin.config import read_config
from src.db.models import Agent, AgentVersion, VersionStatus
from src.models.model_catalog import ASSIGNABLE_MODELS, default_node_model
from src.models.node_registry import NodeType
from src.models.solution_spec import AgentSolutionSpecification
from src.models.workflow import NodeInstance, WorkflowJSON

#: The bundle format, stamped into every export and checked on every import.
#:
#: A version rather than a bare shape, so a file written by a later build is
#: **refused** instead of half-applied. Half-applying is the failure worth
#: designing against here: an import that silently ignored a field it did not
#: recognise would create an agent that is quietly not the one exported, and
#: nothing downstream would ever say so.
BUNDLE_FORMAT = "agentlab.agent/1"


class BundleError(ValueError):
    """A file that cannot become an agent. Message is user-facing."""


@dataclass(frozen=True, slots=True)
class ImportedAgent:
    """What an import produced, plus everything it had to change to produce it.

    `remapped_models` is not decoration. The importing platform's allowlist is
    not the exporting one's, and a creator who is not told that three of their
    steps changed model has been handed a different agent than the one in the
    file. Reported per node rather than as a count, because the next thing they
    do is go and look at those nodes.
    """

    agent: Agent
    version: AgentVersion
    remapped_models: dict[str, tuple[str, str]] = field(default_factory=dict)


def export_bundle(agent: Agent, version: AgentVersion) -> dict[str, object]:
    """`version` as a portable JSON document.

    Refuses a version that has no graph rather than exporting half a bundle. A
    file whose `workflow` was `null` would import into an agent with nothing to
    run, which is a worse outcome than being told now that this version is not
    at a stage worth exporting.

    The stored JSON is passed through the models on the way out — the defence in
    depth ADR-0003 §7 asks of every reader — so a row that predates a schema
    change fails here, loudly, rather than becoming a file that fails on import
    somewhere else with no way to trace where it came from.
    """
    if version.solution_spec is None or version.workflow is None:
        raise BundleError(
            f"Version {version.id} chưa đủ tài liệu để xuất — cần cả solution spec và workflow"
        )
    return {
        "format": BUNDLE_FORMAT,
        "name": agent.name,
        "source_version_id": version.id,
        "solution_spec": AgentSolutionSpecification.model_validate(version.solution_spec).model_dump(mode="json"),
        "workflow": WorkflowJSON.model_validate(version.workflow).model_dump(mode="json"),
    }


def import_bundle(
    db: Session,
    *,
    owner_id: int,
    name: str,
    spec: AgentSolutionSpecification,
    workflow: WorkflowJSON,
) -> ImportedAgent:
    """Create a new agent from a bundle, landing on an editable canvas.

    The version arrives at `workflow_pending_confirmation` and **not** at
    `workflow_confirmed`, which is the whole shape of this feature. An import is
    a graph this platform has never validated, naming resources it may not have;
    landing it confirmed would mean an agent that skipped the one gate every
    other agent passes. Landing it editable means the creator opens the canvas,
    sees precisely what does not resolve here, fixes it, and confirms it the
    ordinary way.

    `solution_spec` is carried across confirmed, unlike the graph, and the
    asymmetry is deliberate: the business problem is the creator's own statement
    and does not stop being true in another deployment, whereas the graph makes
    claims about *this* platform's documents, connections and models.

    The interview is bypassed and that is the point — it exists to produce a
    spec, and an import already has one. What is not bypassed is the model
    allowlist, re-applied below for the reason `create_draft` re-applies it
    (ADR-0010 §4): being the only door in is what makes a gate a gate.
    """
    allowed = list(read_config(db).model_allowlist)
    graph, remapped = _within_allowlist(workflow, allowed)

    agent = Agent(owner_id=owner_id, name=name.strip() or "Imported agent")
    db.add(agent)
    db.flush()

    version = AgentVersion(
        agent_id=agent.id,
        solution_spec=spec.model_dump(mode="json"),
        workflow=graph.model_dump(mode="json"),
        status=VersionStatus.WORKFLOW_PENDING_CONFIRMATION,
    )
    db.add(version)
    db.commit()
    db.refresh(agent)
    db.refresh(version)
    return ImportedAgent(agent=agent, version=version, remapped_models=remapped)


def _within_allowlist(
    workflow: WorkflowJSON, allowed: list[str]
) -> tuple[WorkflowJSON, dict[str, tuple[str, str]]]:
    """`workflow` with every out-of-allowlist node model replaced, and what changed.

    The rule is `src.workflow.composition._assigned_model`'s, applied to a graph
    somebody else wrote instead of to one the meta-agent just wrote: keep the
    named model when this platform both vets and allows it, otherwise fall back
    to `default_node_model`. Stating it the same way in both places is what stops
    an imported graph being buildable in a shape a generated one could not be.

    Only `agent_llm` nodes carry a model. Walking every node type and asking for
    a `model` key would also rewrite a config field that happens to be spelled
    that way on some future node type, which is a rename nobody asked for.
    """
    fallback = default_node_model(allowed)
    remapped: dict[str, tuple[str, str]] = {}
    nodes: list[NodeInstance] = []

    for node in workflow.nodes:
        written = node.config.get("model")
        if node.type is not NodeType.AGENT_LLM or not isinstance(written, str):
            nodes.append(node)
            continue
        named = written.strip()
        if named in allowed and named in ASSIGNABLE_MODELS:
            nodes.append(node)
            continue
        remapped[node.id] = (named, fallback)
        nodes.append(node.model_copy(update={"config": {**node.config, "model": fallback}}))

    if not remapped:
        return workflow, {}
    return workflow.model_copy(update={"nodes": nodes}), remapped
