"""Whether a graph's `data_query` nodes name Hệ CSDL their creator may read — checked at confirm. DS-04.

`src.workflow.knowledge_bases.knowledge_base_problems`' rule for a
`knowledge_search` node, applied to a Hệ CSDL: a `source_id` is creator-typed
text until something resolves it, and confirm is where it is resolved once —
the source exists, it is this creator's, and every view the node reads is one
the creator ticked. The reader checks all of it again on every read; this is
what keeps a graph that can never read anything from being confirmed at all.

Beside `ensure_confirmable` rather than inside it for that module's reason: the
pure graph check needs no database, and this one does.
"""

from collections.abc import Iterator

from pydantic import ValidationError
from sqlalchemy.orm import Session

from src.db.models import DatabaseSource
from src.models.node_registry import DataQueryConfig, NodeType
from src.models.workflow import NodeInstance, WorkflowJSON
from src.workflow.validation import GraphProblem, WorkflowGraphError


def database_source_problems(db: Session, workflow: WorkflowJSON, owner_id: int) -> tuple[GraphProblem, ...]:
    """Every `data_query` node whose `source_id` is not `owner_id`'s, or whose `views` are not all ticked there.

    A node whose config does not parse is skipped — `src.workflow.validation`
    already names it, and a blank `source_id` is `unfilled_fields`' sentence,
    not this one's.
    """
    problems: list[GraphProblem] = []
    for node, config in _data_query_nodes(workflow):
        if not config.source_id:
            continue
        source = owned_source(db, config.source_id, owner_id)
        if source is None:
            problems.append(
                GraphProblem(
                    f"Node {node.id!r} trỏ tới Hệ CSDL {config.source_id!r} không tồn tại hoặc không thuộc về bạn",
                    node_id=node.id,
                )
            )
            continue
        unticked = [view for view in config.views if view not in source.allowed_views]
        if unticked:
            problems.append(
                GraphProblem(
                    f"Node {node.id!r} đọc view chưa được tick ở Hệ CSDL {source.name!r}: {', '.join(unticked)}",
                    node_id=node.id,
                )
            )
    return tuple(problems)


def ensure_database_sources_owned(db: Session, workflow: WorkflowJSON, owner_id: int) -> None:
    """Raise `WorkflowGraphError` naming every problem — `ensure_knowledge_bases_owned`'s shape and prefix."""
    problems = database_source_problems(db, workflow, owner_id)
    if problems:
        raise WorkflowGraphError("Workflow chưa hợp lệ: " + "; ".join(problem.message for problem in problems))


def owned_source(db: Session, source_id: str, owner_id: int) -> DatabaseSource | None:
    """The Hệ CSDL `source_id` names if it exists and is `owner_id`'s — else `None`, `_owned_knowledge_base`'s rule.

    A `source_id` not even shaped like an id is the same "no" as one naming
    nothing or naming somebody else's: from the node's side all three are a
    source it cannot read.
    """
    try:
        source = db.get(DatabaseSource, int(source_id))
    except ValueError:
        return None
    return source if source is not None and source.owner_id == owner_id else None


def _data_query_nodes(workflow: WorkflowJSON) -> Iterator[tuple[NodeInstance, DataQueryConfig]]:
    """Every `data_query` node whose config parses — `src.workflow.validation` names the ones that do not."""
    for node in workflow.nodes_of_type(NodeType.DATA_QUERY):
        try:
            yield node, DataQueryConfig.model_validate(node.config)
        except ValidationError:
            continue
