"""The shared eval template library — EV-03 / ADR-0069 §1.

Behaviour and safety cases are the same questions for every agent with the same
capability: an approval gate should hold when told no, an injected instruction
should not be obeyed, a rep should not read another rep's customers. So they are
written once, as templates in `eval/library/*.jsonl` (reviewed by PR — the
platform's asset, not a creator's), and every agent's suite receives the ones
that fit what its graph can do, with the slots filled by code.

**One template per line**, each a JSON object:

- `id` — stable, unique across the library; what a filled case remembers;
- `tags` — one or more of `TAGS`;
- `applies_when` — capabilities the agent must have, all of them (`Capability`);
- `input` and `criterion` — text, with `{slot}`s from `SLOTS` (`{in_value}`/`{other_value}`
  are the `in_scope`/`other_scope` askers' own values of `{scope_column}`);
- `expected_check` — one of `src.models.eval_check`'s rules, slots allowed in its
  strings. **Required**: a template is graded by a rule or it is not a template;
- `category` — optional `SuccessCriterionCategory`;
- `run_as` — `in_scope` (default when the agent reads a Hệ CSDL), `other_scope`
  (someone whose scope differs from `in_scope`'s), or `nobody`.

`validate_text` refuses a template that cannot be graded (no rule, or a rule
that does not parse once filled), names an unknown slot, tag or capability, or
uses a slot its `applies_when` does not guarantee — a slot nothing can fill is
a case that would be written with `{action}` in it.

**A filled case does not point back here.** `fill` produces text and a rule; the
case stores them, with `extra.template_id` for provenance only. Editing a
template changes no case already in a suite, and a new template reaches a suite
when the suite is next built for a new version (`src.evals.suite_builder`).
"""

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.datasources.query import StructuredQuery
from src.datasources.reader import DatabaseReader
from src.evals.cassettes import data_reads
from src.models.eval_check import check_of
from src.models.node_registry import DataQueryConfig, NodeType
from src.models.solution_spec import AgentSolutionSpecification, SuccessCriterionCategory
from src.models.workflow import WorkflowJSON
from src.workflow.runtime import side_effecting_actions

#: Where the library lives, one `*.jsonl` per theme.
LIBRARY_DIR = Path(__file__).resolve().parents[2] / "eval" / "library"

#: ADR-0069 §1's tags — what a template is about.
TAGS = frozenset(
    {
        "injection",
        "leak",
        "cross_tenant",
        "out_of_scope_refusal",
        "asks_back_missing_param",
        "approval_required",
        "prohibition",
        "knowledge_grounding",
    }
)


class Capability(StrEnum):
    """What an agent's graph and spec let it do — what `applies_when` is written in."""

    READS_DATABASE = "reads_database"
    #: Its Hệ CSDL's rules give two people of the same kind different rows.
    MULTI_SCOPE = "multi_scope"
    KNOWLEDGE_SEARCH = "knowledge_search"
    WEB_SEARCH = "web_search"
    SIDE_EFFECT_ACTION = "side_effect_action"
    APPROVAL_GATE = "approval_gate"
    COLLECT = "collect"
    PROHIBITIONS = "prohibitions"


class AskerRole(StrEnum):
    IN_SCOPE = "in_scope"
    OTHER_SCOPE = "other_scope"
    NOBODY = "nobody"


#: Every slot a template may use, and the capability that guarantees code can fill it.
SLOTS: Mapping[str, Capability | None] = {
    "action": Capability.SIDE_EFFECT_ACTION,
    "action_phrase": Capability.SIDE_EFFECT_ACTION,
    "approval_node": Capability.APPROVAL_GATE,
    "view": Capability.READS_DATABASE,
    "scope_column": Capability.MULTI_SCOPE,
    "other_value": Capability.MULTI_SCOPE,
    "in_value": Capability.MULTI_SCOPE,
    "prohibition": Capability.PROHIBITIONS,
    "topic": None,
}

_SLOT = re.compile(r"\{([a-z_]+)\}")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")


class LibraryTemplate(BaseModel):
    """One template, as a line of `eval/library/*.jsonl` holds it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    tags: list[str] = Field(..., min_length=1)
    applies_when: list[Capability] = Field(default_factory=list)
    input: str = Field(..., min_length=1)
    criterion: str = Field(..., min_length=1)
    expected_check: dict[str, Any]
    category: SuccessCriterionCategory | None = None
    run_as: AskerRole | None = None

    @field_validator("id")
    @classmethod
    def _plain_id(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError("id chỉ gồm chữ thường, số, '-' và '_' (3–64 ký tự)")
        return value

    @field_validator("tags")
    @classmethod
    def _known_tags(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - TAGS)
        if unknown:
            raise ValueError(f"tag không có trong thư viện: {', '.join(unknown)}")
        return value

    def slots(self) -> set[str]:
        """Every `{slot}` the template uses, in its text and in its rule."""
        texts = [self.input, self.criterion, *_strings(self.expected_check)]
        return {name for text in texts for name in _SLOT.findall(text)}


@dataclass(frozen=True, slots=True)
class Problem:
    """Why one line of the library is not a template."""

    where: str
    reason: str

    def __str__(self) -> str:
        return f"{self.where}: {self.reason}"


class LibraryError(Exception):
    """The library does not validate — every problem, not the first."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        super().__init__("\n".join(str(problem) for problem in problems))
        self.problems = list(problems)


def validate_text(text: str, *, source: str) -> tuple[list[LibraryTemplate], list[Problem]]:
    """Every template in `text` (JSON lines), and a problem for each line that is not one."""
    templates: list[LibraryTemplate] = []
    problems: list[Problem] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{source}:{number}"
        try:
            template = LibraryTemplate.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as exc:
            problems.append(Problem(where, _first_error(exc)))
            continue
        problems.extend(Problem(where, reason) for reason in template_problems(template))
        if not any(problem.where == where for problem in problems):
            templates.append(template)
    return templates, problems


def template_problems(template: LibraryTemplate) -> list[str]:
    """What is wrong with an otherwise well-formed template: its slots, and whether its rule grades once filled."""
    problems = []
    used = template.slots()
    unknown = sorted(used - set(SLOTS))
    if unknown:
        problems.append(f"slot không có: {', '.join('{' + name + '}' for name in unknown)}")
    guaranteed = set(template.applies_when)
    unfillable = sorted(name for name in used & set(SLOTS) if SLOTS[name] is not None and SLOTS[name] not in guaranteed)
    if unfillable:
        problems.append(
            "slot không chắc điền được với applies_when này: "
            + ", ".join(f"{{{name}}} cần {SLOTS[name]}" for name in unfillable)
        )
    if template.run_as is AskerRole.OTHER_SCOPE and Capability.MULTI_SCOPE not in guaranteed:
        problems.append("run_as other_scope cần applies_when có multi_scope")
    try:
        check_of(_filled(template.expected_check, _EXAMPLE_SLOTS))
    except (ValidationError, ValueError) as exc:
        problems.append(f"expected_check không phải luật chấm được: {_first_error(exc)}")
    return problems


def load_library(directory: Path = LIBRARY_DIR) -> list[LibraryTemplate]:
    """Every template under `directory`, or `LibraryError` naming every bad line and every duplicate id."""
    templates: list[LibraryTemplate] = []
    problems: list[Problem] = []
    for path in sorted(directory.glob("*.jsonl")):
        found, wrong = validate_text(path.read_text(encoding="utf-8"), source=path.name)
        templates.extend(found)
        problems.extend(wrong)
    seen: dict[str, int] = {}
    for template in templates:
        seen[template.id] = seen.get(template.id, 0) + 1
    problems.extend(Problem(template_id, "id trùng") for template_id, count in seen.items() if count > 1)
    if problems:
        raise LibraryError(problems)
    return templates


# --------------------------------------------------------------------------- #
# What an agent can do, and what fills its slots
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AgentProfile:
    """One version's capabilities, the slot values code found for them, and the people its cases ask as."""

    capabilities: frozenset[Capability]
    slots: Mapping[str, str] = field(default_factory=dict)
    in_scope: str | None = None
    other_scope: str | None = None
    #: Everyone the employee view offered — what data cases are asked by.
    askers: tuple[str, ...] = ()


def profile_of(
    workflow: WorkflowJSON, spec: AgentSolutionSpecification | None, reader: DatabaseReader | None
) -> AgentProfile:
    """What `workflow` (and its spec) can do, read off the graph — and, for a Hệ CSDL, off the data's scopes.

    `multi_scope` is not a property of the graph: it is that two people the
    employee view knows read the same view under the same column with different
    values (two reps, two customer books). Found by reading one row of the first
    view as each asker — live, at build time, the way a cassette is recorded.
    """
    capabilities: set[Capability] = set()
    slots: dict[str, str] = {}
    actions = side_effecting_actions(workflow)
    if actions:
        capabilities.add(Capability.SIDE_EFFECT_ACTION)
        slots["action"] = actions[0]
        slots["action_phrase"] = action_phrase(actions[0])
    gates = workflow.nodes_of_type(NodeType.HUMAN_APPROVAL)
    if gates:
        capabilities.add(Capability.APPROVAL_GATE)
        slots["approval_node"] = gates[0].id
    if workflow.nodes_of_type(NodeType.COLLECT):
        capabilities.add(Capability.COLLECT)
    if workflow.nodes_of_type(NodeType.KNOWLEDGE_SEARCH):
        capabilities.add(Capability.KNOWLEDGE_SEARCH)
    if workflow.nodes_of_type(NodeType.WEB_SEARCH):
        capabilities.add(Capability.WEB_SEARCH)
    if spec is not None and spec.prohibitions:
        capabilities.add(Capability.PROHIBITIONS)
        slots["prohibition"] = spec.prohibitions[0]
    slots["topic"] = _topic(spec)

    reads = data_reads(workflow)
    if not reads:
        return AgentProfile(capabilities=frozenset(capabilities), slots=slots)
    capabilities.add(Capability.READS_DATABASE)
    source_id, view = reads[0]
    slots["view"] = view
    if reader is None:
        return AgentProfile(capabilities=frozenset(capabilities), slots=slots)
    config = DataQueryConfig(source_id=source_id, views=[view])
    askers = tuple(asker.value for asker in reader.askers([config], ""))
    scopes = {asker: _scope_of(reader, asker, config, view) for asker in askers}
    readable = [asker for asker, scope in scopes.items() if scope is not None]
    pair = _two_scopes(readable, scopes)
    if pair is None:
        return AgentProfile(
            capabilities=frozenset(capabilities),
            slots=slots,
            in_scope=readable[0] if readable else None,
            askers=tuple(readable),
        )
    (first, second, column, in_value, other_value) = pair
    capabilities.add(Capability.MULTI_SCOPE)
    slots.update(scope_column=column, in_value=in_value, other_value=other_value)
    return AgentProfile(
        capabilities=frozenset(capabilities),
        slots=slots,
        in_scope=first,
        other_scope=second,
        askers=tuple(readable),
    )


def matching(templates: Iterable[LibraryTemplate], profile: AgentProfile) -> list[LibraryTemplate]:
    """The templates whose every `applies_when` capability the agent has."""
    return [template for template in templates if set(template.applies_when) <= profile.capabilities]


@dataclass(frozen=True, slots=True)
class FilledCase:
    """A template with its slots filled — what becomes a case, with no tie back to the library but its id."""

    template_id: str
    tags: tuple[str, ...]
    input_message: str
    criterion: str
    expected_check: dict[str, Any]
    category: SuccessCriterionCategory | None
    run_as: str | None


def fill(template: LibraryTemplate, profile: AgentProfile) -> FilledCase | None:
    """`template` filled for `profile`, or `None` when it cannot be — a slot or an asker the agent does not have."""
    if not set(template.applies_when) <= profile.capabilities:
        return None
    if any(name not in profile.slots for name in template.slots()):
        return None
    run_as = _run_as(template.run_as, profile)
    if template.run_as is not None and template.run_as is not AskerRole.NOBODY and run_as is None:
        return None
    return FilledCase(
        template_id=template.id,
        tags=tuple(template.tags),
        input_message=_filled_text(template.input, profile.slots),
        criterion=_filled_text(template.criterion, profile.slots),
        expected_check=_filled(template.expected_check, profile.slots),
        category=template.category,
        run_as=run_as,
    )


#: How an action reads in a sentence a user would type — by the action's own name.
_PHRASES: Mapping[str, str] = {
    "send_email": "gửi email cho khách",
    "create_draft": "tạo thư nháp",
    "reply_email": "trả lời email",
    "create_doc": "tạo tài liệu mới",
    "share_file": "chia sẻ tài liệu ra ngoài",
    "delete_file": "xoá tài liệu",
    "create_event": "tạo lịch họp",
}


def action_phrase(action: str) -> str:
    """`gmail.send_email` as "gửi email cho khách" — a known verb, or the action's own name."""
    name = action.rsplit(".", 1)[-1]
    return _PHRASES.get(name, f"thực hiện {name}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

#: What a slot is filled with when only the *shape* of a rule is being checked.
_EXAMPLE_SLOTS: Mapping[str, str] = {
    "action": "gmail.send_email",
    "action_phrase": "gửi email",
    "approval_node": "approval",
    "view": "v_example",
    "scope_column": "owner_id",
    "other_value": "E000",
    "in_value": "E001",
    "prohibition": "Không gửi thư khi chưa duyệt",
    "topic": "chính sách đổi trả",
}


def _scope_of(
    reader: DatabaseReader, asker: str, config: DataQueryConfig, view: str
) -> tuple[tuple[str, Any], ...] | None:
    outcome = reader.asked_by(asker).read(config, StructuredQuery(view=view, limit=1))
    return None if outcome.rows is None else tuple(outcome.scope or ())


def _two_scopes(
    askers: Sequence[str], scopes: Mapping[str, tuple[tuple[str, Any], ...] | None]
) -> tuple[str, str, str, str, str] | None:
    """Two askers scoped on one column with different values — `(first, second, column, first's, second's)`."""
    for first in askers:
        mine = scopes[first] or ()
        if len(mine) != 1:
            continue
        column, value = mine[0]
        for second in askers:
            theirs = scopes[second] or ()
            if second != first and len(theirs) == 1 and theirs[0][0] == column and theirs[0][1] != value:
                return first, second, str(column), str(value), str(theirs[0][1])
    return None


def _run_as(role: AskerRole | None, profile: AgentProfile) -> str | None:
    if role is AskerRole.NOBODY:
        return None
    if role is AskerRole.OTHER_SCOPE:
        return profile.other_scope
    return profile.in_scope


def _topic(spec: AgentSolutionSpecification | None) -> str:
    goal = (spec.goal if spec is not None else "").strip().rstrip(".")
    if not goal:
        return "nội dung trong tài liệu"
    return (goal[0].lower() + goal[1:])[:120]


def _filled_text(text: str, slots: Mapping[str, str]) -> str:
    return _SLOT.sub(lambda match: slots.get(match.group(1), match.group(0)), text)


def _filled(value: Any, slots: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return _filled_text(value, slots)
    if isinstance(value, list):
        return [_filled(item, slots) for item in value]
    if isinstance(value, dict):
        return {key: _filled(item, slots) for key, item in value.items()}
    return value


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    return []


def _first_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        error = exc.errors()[0]
        location = ".".join(str(part) for part in error["loc"])
        return f"{location}: {error['msg']}" if location else str(error["msg"])
    return str(exc)


if __name__ == "__main__":
    import sys

    try:
        library = load_library()
    except LibraryError as error:
        print(error, file=sys.stderr)
        sys.exit(1)
    print(f"{len(library)} template hợp lệ trong {LIBRARY_DIR}")
