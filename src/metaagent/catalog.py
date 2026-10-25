"""The creator's tool catalog, in the shape every meta-agent prompt reads it.

One builder and one renderer, shared by the workbench loop (ADR-0050) and the
Workflow Composer (ADR-0041), so that what the two models are told about an
action cannot drift apart — and so that both are told the one thing the
prompts used to leave out: **what the action takes**.

The field report that put the schema here: an agent built to send invitations
had its "đã đủ thông tin?" gate written as *"tên sự kiện, thời gian, địa điểm,
tên người chủ trì, email người chủ trì, tên người được mời, email người được
mời"*. `gmail.send_email` takes `to`, `subject`, `body`. The sender is the
connected account. So a user who gave everything the action could use was
told "thiếu email người chủ trì" and the run ended — on a field that had no
parameter to go into. The model that wrote the gate had never been shown the
parameters; it read "từ người chủ trì" three times in the spec and did the
obvious thing. Showing it the parameters is the fix at the level where the
mistake was made.
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from src.metaagent.loop import CatalogAction, CatalogTool
from src.workflow.registrations import approved_registrations
from src.workflow.tools import TOOL_DEFINITIONS, ToolStatus


def creator_catalog(db: Session, creator_id: int) -> tuple[CatalogTool, ...]:
    """Every connection this creator may wire — names and schemas, never credentials.

    The same two halves `list_selectable_tools` reads, deliberately: what a
    meta agent may propose and what the canvas dropdown offers must be one
    list, or a proposal could name a connection the Inspector then cannot show.
    """
    built_in = [tool for tool in TOOL_DEFINITIONS.all() if tool.status is ToolStatus.APPROVED]
    reachable = [*built_in, *approved_registrations(db, for_creator=creator_id)]
    return tuple(
        CatalogTool(
            id=tool.id,
            name=tool.name,
            actions=tuple(
                CatalogAction(
                    name=action.name,
                    description=action.description,
                    read_only=action.read_only,
                    risk=action.risk_level.value,
                    input_schema=dict(action.input_schema),
                )
                for action in tool.actions
            ),
        )
        for tool in reachable
    )


def parameters_of(input_schema: Mapping[str, Any]) -> str:
    """`to*, subject*, body*` — an action's parameters on one line, required ones starred.

    Names only. A type or a description per parameter would be more to read
    for a decision that turns on one question: *which details exist at all*.
    """
    properties = input_schema.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        return "không có tham số"
    required = set(input_schema.get("required") or ())
    return ", ".join(f"{name}*" if name in required else str(name) for name in properties)


def catalog_lines(catalog: tuple[CatalogTool, ...]) -> list[str]:
    """One line per action: id, effect, what it does, and what it takes."""
    lines = []
    for tool in catalog:
        for action in tool.actions:
            effect = "read-only" if action.read_only else f"WRITE, risk {action.risk}"
            lines.append(
                f"- {tool.id}.{action.name} ({effect}) — {action.description}; "
                f"tham số: {parameters_of(action.input_schema)}"
            )
    return lines
