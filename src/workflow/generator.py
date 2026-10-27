"""One confirmed business problem in, one drafted graph out — the Generator, #43.

Take a spec the creator confirmed, produce a graph for it, and leave the result
on the version at `workflow_pending_confirmation` for the canvas to pick up.
**How** the graph is produced is the one decision this module makes, and since
ADR-0041 there are two answers to it:

- **composed** (`composition`) when the creator asked for a workflow and named no
  template. A model draws a graph for *their* problem out of the eight node
  types, and the graph validator decides whether what it drew can run. This is
  the default, because a creator describing a business is describing that
  business rather than shopping for a shape.
- **from a template** (`fill`) when the creator picked one. The catalog is now a
  gallery they choose from — the convenient way to start a common agent — rather
  than a menu the platform matches them to behind their back. Nothing about the
  shape is decided by a model on this path; only the wording is.

`selection` is the third path, and it is now a **fallback and nothing else**:
composition failing after its attempts leaves a creator staring at an error where
a draft should be, so the platform picks the closest template rather than
nothing. Requirement §2's promise was that an imperfect draft beats no draft, and
that promise is what survives of ADR-0025 §4.

Two gates, and they are the same gate at both ends of this module. It only reads
a version at `spec_confirmed`, because ADR-0020 §4 promises the Generator a spec
that is complete and will not move under it. And it refuses to run twice, because
the document it would overwrite is one the creator has been editing on the canvas
since — a re-draft is not a retry, it is a silent discard of their work. Both
refusals are `WorkflowStageError`, and both are sentences a screen can show.

Shaped after `src.solution.specs`, which does the same job one stage earlier: a
stage gate, a document written to `AgentVersion`, and one re-validating reader
(`workflow_of`) so a stored row never reaches anybody unchecked. The two live in
separate modules because they gate *different documents* — a version at
`spec_confirmed` has no workflow at all, and none of the spec's transitions
apply to it.
"""

import logging
from collections.abc import Sequence

from sqlalchemy.orm import Session

from src.admin.config import read_config
from src.agents.versions import wrong_stage
from src.datasources.graph import owned_source
from src.datasources.sources import access_rules_of
from src.db.models import AgentVersion, VersionStatus
from src.metaagent.catalog import creator_catalog
from src.metaagent.loop import CatalogTool
from src.models.node_registry import NodeType
from src.models.solution_spec import AgentSolutionSpecification, DataSourceKind
from src.models.workflow import WorkflowJSON
from src.services.llm import ChatModelFactory
from src.solution.specs import solution_spec_of
from src.workflow.composition import GraphCompositionError, compose_workflow
from src.workflow.fill import fill_config
from src.workflow.selection import select_template
from src.workflow.templates import TemplateSpec, template_of

_log = logging.getLogger(__name__)

#: Said when a creator asks to start from a template that is not on offer. Names
#: what they can do instead, because the id came from a gallery they were looking
#: at and the honest reading is that it has since been disabled or rejected —
#: not that they typed something wrong.
_NO_SUCH_TEMPLATE = (
    "Không có mẫu workflow nào tên {template_id!r} đang được dùng. "
    "Bạn chọn lại một mẫu trong thư viện, hoặc để hệ thống tự dựng workflow từ đặc tả của bạn."
)


class WorkflowStageError(ValueError):
    """A workflow was asked for something its stage does not allow. Message is user-facing."""


async def draft_workflow(
    db: Session,
    version: AgentVersion,
    *,
    model_factory: ChatModelFactory,
    template_id: str | None = None,
) -> AgentVersion:
    """Draft `version`'s workflow from the spec it already holds confirmed.

    `template_id` is the creator's own choice and the only thing that decides
    which path runs. `None` composes; an id builds that template. There is no
    third state and nothing is inferred — a platform that sometimes composed and
    sometimes reached for a template depending on how well a description matched
    would be one whose output nobody could predict from what they typed.

    Committed as `workflow_pending_confirmation`, never as anything further
    along. What comes back is a draft — a prompt the model declined to write, an
    `action` nobody has bound yet, a condition still blank are all ordinary at
    this stage (ADR-0022 §5), and the canvas is where they get fixed.
    """
    _require_status(version, VersionStatus.SPEC_CONFIRMED, action="dựng workflow")
    spec = solution_spec_of(version)
    allowlist = read_config(db).model_allowlist
    if template_id is None:
        workflow = await _composed(
            db,
            spec,
            model_factory=model_factory,
            model_allowlist=allowlist,
            catalog=creator_catalog(db, version.agent.owner_id),
        )
    else:
        workflow = await _from_template(
            _chosen(db, template_id), spec, model_factory=model_factory, model_allowlist=allowlist
        )

    workflow = bound_databases(db, workflow, spec, owner_id=version.agent.owner_id)
    version.workflow = workflow.model_dump(mode="json")
    version.status = VersionStatus.WORKFLOW_PENDING_CONFIRMATION
    db.commit()
    db.refresh(version)
    return version


async def _composed(
    db: Session,
    spec: AgentSolutionSpecification,
    *,
    model_factory: ChatModelFactory,
    model_allowlist: Sequence[str],
    catalog: tuple[CatalogTool, ...] = (),
) -> WorkflowJSON:
    """A graph drawn for `spec`, falling back to the closest template if none could be.

    The fallback is the whole reason this function exists rather than a bare call
    to `compose_workflow`. A creator who has just finished an interview and
    confirmed a spec is owed *something to edit*; a composition that failed three
    times is the platform's problem, and answering it with an error message would
    make it theirs. The closest template is a worse draft than a composed graph
    and a far better one than an empty canvas.

    Logged at warning, because a fallback that happens silently is a regression
    in the composer nobody would ever notice — the creator sees a workflow either
    way.
    """
    try:
        return await compose_workflow(
            spec=spec, model_factory=model_factory, model_allowlist=model_allowlist, catalog=catalog
        )
    except GraphCompositionError as exc:
        _log.warning("Không soạn được workflow, quay về mẫu gần nhất. Lý do: %s", exc.reason)
        reason = exc.reason
    template = await select_template(spec=spec, model_factory=model_factory, db=db)
    built = await _from_template(template, spec, model_factory=model_factory, model_allowlist=model_allowlist)
    # On the document, not only in the log: the canvas turns it into a warning
    # (`graph_warnings`), so the creator reads why a template appeared where a
    # graph about their business should have.
    return built.model_copy(update={"metadata": built.metadata.model_copy(update={"fallback_reason": reason})})


async def _from_template(
    template: TemplateSpec,
    spec: AgentSolutionSpecification,
    *,
    model_factory: ChatModelFactory,
    model_allowlist: Sequence[str],
) -> WorkflowJSON:
    """`template`'s fixed shape with this spec's resources and wording in it."""
    return await fill_config(template=template, spec=spec, model_factory=model_factory, model_allowlist=model_allowlist)


def bound_databases(
    db: Session, workflow: WorkflowJSON, spec: AgentSolutionSpecification, *, owner_id: int
) -> WorkflowJSON:
    """Each `data_query` node pointed at the Hệ CSDL the spec names, with its views — by rule, never by a model (DS-11).

    The n-th `data_query` node reads the n-th `database` source, the way the
    spec lists them. `source_id` is that source's `ref`; `views` are the
    views the creator ticked on it, less the employee view its access rules
    resolve askers against — that one is for deciding scope, not for reading.
    A source with no `ref` yet, or one that is not the owner's, leaves its
    node blank for the canvas to ring, as an unbound `tool_id` is.
    """
    refs = [source.ref for source in spec.data if source.kind is DataSourceKind.DATABASE]
    nodes, index = [], 0
    for node in workflow.nodes:
        if node.type is not NodeType.DATA_QUERY:
            nodes.append(node)
            continue
        ref = refs[index] if index < len(refs) else None
        index += 1
        source = owned_source(db, ref, owner_id) if ref else None
        if source is None:
            nodes.append(node)
            continue
        rules = access_rules_of(source)
        identity_view = rules.identity.view if rules is not None else None
        views = [view for view in source.allowed_views if view != identity_view]
        nodes.append(node.model_copy(update={"config": {**node.config, "source_id": str(ref), "views": views}}))
    return workflow.model_copy(update={"nodes": nodes})


def _chosen(db: Session, template_id: str) -> TemplateSpec:
    """The template the creator named, or a refusal saying it is not on offer.

    Resolved through `template_of`, which is what makes "on offer" mean the same
    thing here as it does in the gallery: a `pending`, `rejected` or `disabled`
    entry is not found, so a creator cannot build on a template an admin has
    taken down by holding on to its id.
    """
    template = template_of(template_id, db)
    if template is None:
        raise WorkflowStageError(_NO_SUCH_TEMPLATE.format(template_id=template_id))
    return template


def workflow_of(version: AgentVersion) -> WorkflowJSON:
    """`version`'s workflow, re-validated on the way out.

    Re-validated rather than handed over as stored JSON, the defence in depth
    ADR-0003 §7 asks of every stored document: a row written before a validator
    existed must not reach a reader either.

    A version with no workflow is refused by name. `agent_versions` carries V1
    rows and V2 rows at the spec stage, and a caller that asked the wrong one of
    three documents deserves a sentence saying so rather than a validation error
    about a document that was never there.
    """
    if version.workflow is None:
        raise WorkflowStageError("Version này chưa có workflow nào")
    return WorkflowJSON.model_validate(version.workflow)


def _require_status(version: AgentVersion, expected: VersionStatus, *, action: str) -> None:
    """Refuse unless `version` is at the one stage where `action` is allowed.

    The sentence comes from `wrong_stage`, shared with the spec's own gate
    (#42); the exception type stays this module's, because a caller catching a
    refusal about the workflow must not also catch one about the spec.
    """
    refusal = wrong_stage(version, expected, action=action)
    if refusal is not None:
        raise WorkflowStageError(refusal)
