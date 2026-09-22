"""UC-1's tool catalog (FR-15, T9) — the fixed list `capabilities[].tool` may name.

Declared in code, not proposed by the meta-agent (Hard rule #2 of
`platform/meta-agent-system-prompt.md`): the drafter may only *pick* a name from
here, and `validate_spec` — the one gate every writer of a spec passes through
(ADR-0047) — refuses anything else, including a plausible-sounding tool that was
never registered (`erp.query_all`). That is what makes the rejection reach the API
layer rather than only a UI that happens to render this list as a dropdown.

This is UC-1's own catalog, distinct from `src.agents.tools.registry.TOOL_REGISTRY`:
that one names V2's generic tools (`web_search`, `db_query`, ...); this one names
the five tools `agentspec.yaml`'s capabilities actually use, in the `domain.action`
shape `agentspec.schema.json` requires.
"""

from dataclasses import dataclass

from src.agentspec.spec import Scope


@dataclass(frozen=True)
class ToolCatalogEntry:
    """One entry a spec's `capabilities[].tool` is allowed to name."""

    name: str
    description: str
    scope: Scope


CATALOG: tuple[ToolCatalogEntry, ...] = (
    ToolCatalogEntry(
        name="erp.query_receivables",
        description="Tra cứu công nợ quá hạn từ ERP.",
        scope=Scope.READ,
    ),
    ToolCatalogEntry(
        name="erp.query_customer",
        description="Tra cứu thông tin khách hàng từ ERP.",
        scope=Scope.READ,
    ),
    ToolCatalogEntry(
        name="kb.search",
        description="Tìm kiếm tài liệu chính sách nội bộ.",
        scope=Scope.READ,
    ),
    ToolCatalogEntry(
        name="mail.send_reminder",
        description="Soạn và gửi email nhắc nợ.",
        scope=Scope.WRITE,
    ),
    ToolCatalogEntry(
        name="task.create",
        description="Tạo task theo dõi trong hệ thống ticket.",
        scope=Scope.WRITE,
    ),
)

_BY_NAME: dict[str, ToolCatalogEntry] = {entry.name: entry for entry in CATALOG}


def catalog_entries() -> tuple[ToolCatalogEntry, ...]:
    """Every tool in the catalogue, for the config form and for the meta-agent prompt."""
    return CATALOG


def known_tool_names() -> frozenset[str]:
    """Every name a spec's `capabilities[].tool` may equal."""
    return frozenset(_BY_NAME)


def is_known_tool(name: str) -> bool:
    return name in _BY_NAME
