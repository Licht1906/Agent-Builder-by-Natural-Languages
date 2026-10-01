"""What the interview must collect, and the arithmetic that ends it.

The deterministic half of the meta-agent, and deliberately the half with no model
call in it. Everything the ticket asks to be *xác định* — which question comes
next, how many are left, whether the interview is over — is decided here by
looking at which slots are empty. A model never gets a vote on any of it.

Two rules do most of the work, and both are structural rather than remembered:

- **A filled slot is never overwritten** (`SlotSheet.with_values`). That is what
  makes *"không hỏi lại thứ người dùng vừa nói"* true even when a later
  extraction pass has an opinion about a slot the creator already settled.
- **A question is only ever "this slot is empty"** (`SlotSheet.next_question`).
  There is no question bank, no follow-up, and no way for the interview to ask
  about something it already knows.

Together they make the interview monotonic: every turn either fills a slot or
leaves the sheet as it was, so the number of remaining questions never rises. The
caller in `src.interview.sessions` supplies the last piece — an answered question
always fills its slot — which is what turns "never rises" into "reaches zero".

The `Slot` members are named after `InterviewSlots`' fields on purpose: the sheet
this module produces is the object #17 hands to `AgentSpec`, and a mapping layer
between the two would be a place for them to disagree about what a slot is called.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from src.models.agent_spec import InterviewSlots

#: A slot holds either one sentence or a list of them — see `SlotSpec.is_list`.
SlotValue = str | tuple[str, ...]


class Slot(StrEnum):
    """The five things the platform must know before it can build an agent.

    Declaration order is interview order, and it is not arbitrary: it runs from
    what a description almost always states (who it is for, what goes in) to what
    a description almost never states (what "correct" means, what is forbidden).
    A creator who abandons the interview part-way has therefore answered the
    questions they were most able to answer, and the gap is at the end where it
    is visible rather than scattered.
    """

    AUDIENCE = "audience"
    INPUTS = "inputs"
    CAPABILITIES = "capabilities"
    SUCCESS_CRITERIA = "success_criteria"
    PROHIBITIONS = "prohibitions"


@dataclass(frozen=True, slots=True)
class SlotSpec:
    """One slot: what it is called, what it asks, and what shape its answer takes.

    `question` is the exact sentence the creator sees. It lives here rather than
    in the API layer because the extraction prompt needs the same words — a model
    told to fill "success_criteria" and a creator asked "thế nào là một câu trả
    lời đúng?" would otherwise be working from two different questions.
    """

    slot: Slot
    question: str
    is_list: bool
    hint: str
    """What a usable answer looks like. Shown to the model, not to the creator —
    it is what stops an extraction pass storing "có" as a success criterion."""


#: The interview, in full. Every part of the **backend** reads this tuple — the
#: progress count, the extraction prompt, the stop condition and the API's slot
#: list — so none of them can fall out of step with the others.
#:
#: It does not reach across the wire. Adding a slot also means touching
#: `InterviewSlotName` and the route guard in `frontend/`, which hold their own
#: copy of these five names — a deliberate duplication (ADR-0014, Consequences)
#: and the reason this comment stops at "the backend".
SLOT_SCHEDULE: tuple[SlotSpec, ...] = (
    SlotSpec(
        slot=Slot.AUDIENCE,
        question="Ai sẽ dùng agent này?",
        is_list=False,
        hint="một nhóm người cụ thể, ví dụ 'nhân viên chăm sóc khách hàng của phòng kinh doanh'",
    ),
    SlotSpec(
        slot=Slot.INPUTS,
        question="Dữ liệu đầu vào của agent đến từ đâu?",
        is_list=False,
        hint="nguồn dữ liệu cụ thể, ví dụ 'email khiếu nại khách gửi vào hộp thư hỗ trợ'",
    ),
    SlotSpec(
        slot=Slot.CAPABILITIES,
        question="Agent cần làm được những việc gì?",
        is_list=True,
        hint="từng việc một, mỗi việc là một hành động agent phải làm được",
    ),
    SlotSpec(
        slot=Slot.SUCCESS_CRITERIA,
        question="Thế nào là một câu trả lời đúng của agent này?",
        is_list=True,
        hint=(
            "từng tiêu chí kiểm được, mỗi tiêu chí phải đọc xong là chấm được đạt hay không — "
            "đây là thứ khâu eval dùng để sinh test case"
        ),
    ),
    SlotSpec(
        slot=Slot.PROHIBITIONS,
        question="Agent tuyệt đối không được làm những gì?",
        is_list=True,
        hint="từng điều cấm một, mỗi điều là một hành động agent không bao giờ được làm",
    ),
)

#: `SLOT_SCHEDULE` keyed by slot, for the lookups that would otherwise be a scan.
SPEC_OF: Mapping[Slot, SlotSpec] = {spec.slot: spec for spec in SLOT_SCHEDULE}


def normalise(slot: Slot, raw: object) -> SlotValue | None:
    """`raw` as a storable value for `slot`, or `None` if it holds nothing.

    The single place that decides whether a slot counts as filled. Extraction, a
    creator's typed answer and a row read back from the database all come through
    here, so "filled" cannot mean three slightly different things depending on
    where a value entered the system — which is the failure that would let a slot
    holding `[""]` be treated as answered and never asked about again.

    Forgiving about shape and strict about content: a list slot accepts a bare
    string (a model that answered with one sentence meant one item), and a scalar
    slot accepts a list (it takes the first usable entry). What neither accepts is
    emptiness after stripping.
    """
    spec = SPEC_OF[slot]
    items = _string_items(raw)
    if not items:
        return None
    return tuple(items) if spec.is_list else items[0]


def _string_items(raw: object) -> list[str]:
    """Every non-empty string inside `raw`, stripped — flattening one level of list.

    Nested lists and non-string scalars are dropped rather than coerced. `str()`
    around whatever a model returned is how `"{'a': 1}"` ends up stored as a
    success criterion, and a criterion nobody can read is worse than a missing one
    because it still counts towards the interview being finished.
    """
    values = raw if isinstance(raw, list) else [raw]
    return [item.strip() for item in values if isinstance(item, str) and item.strip()]


@dataclass(frozen=True, slots=True)
class SlotSheet:
    """Which slots hold something, and therefore what is left to ask.

    Holds only filled slots: an entry's presence *is* its filled-ness, so there is
    no second flag to keep in step with the value. Everything else the interview
    needs — the next question, the progress count, whether it is over — is derived
    from that one fact rather than stored beside it.

    Immutable, and the two ways to change it (`with_values`, `without`) both
    return a new sheet. A session's state is then the sheet it holds, which is
    what lets `sessions` persist one JSON column and re-derive the rest.
    """

    values: Mapping[Slot, SlotValue]

    @classmethod
    def empty(cls) -> "SlotSheet":
        """A sheet before anything has been read into it — five questions to go."""
        return cls(values={})

    @classmethod
    def from_stored(cls, stored: Mapping[str, object]) -> "SlotSheet":
        """The sheet a database row was holding.

        Anything unreadable is dropped rather than raising: an unknown key is a
        slot a later version of the schedule removed, and a value that no longer
        normalises is one whose slot changed shape. Both should cost the creator
        one question, not the whole session they had half-finished.
        """
        values: dict[Slot, SlotValue] = {}
        for name, raw in stored.items():
            slot = _slot_named(name)
            if slot is None:
                continue
            value = normalise(slot, raw)
            if value is not None:
                values[slot] = value
        return cls(values=values)

    def to_stored(self) -> dict[str, str | list[str]]:
        """This sheet as JSON the database column can hold, in schedule order."""
        return {slot.value: list(value) if isinstance(value, tuple) else value for slot, value in self._in_order()}

    def with_values(self, proposals: Mapping[Slot, SlotValue]) -> "SlotSheet":
        """This sheet plus every proposal for a slot that is currently **empty**.

        A proposal for a filled slot is discarded, and that is the whole point.
        Extraction runs again on every answer, and a model reading "gửi cho khách
        hàng doanh nghiệp" while `audience` already says "nhân viên CSKH" would
        otherwise quietly replace what the creator told us with what it inferred.
        Correcting a slot is a thing the creator does deliberately, through
        `without` and a fresh value — never a side effect of answering something
        else.
        """
        added = {slot: value for slot, value in proposals.items() if slot not in self.values}
        return SlotSheet(values={**self.values, **added}) if added else self

    def without(self, slot: Slot) -> "SlotSheet":
        """This sheet with `slot` emptied — the first half of correcting it."""
        if slot not in self.values:
            return self
        return SlotSheet(values={key: value for key, value in self.values.items() if key != slot})

    @property
    def pending(self) -> tuple[SlotSpec, ...]:
        """The slots still empty, in interview order — exactly the questions left."""
        return tuple(spec for spec in SLOT_SCHEDULE if spec.slot not in self.values)

    @property
    def next_question(self) -> SlotSpec | None:
        """The one question to put to the creator now, or `None` if there are none left."""
        pending = self.pending
        return pending[0] if pending else None

    @property
    def remaining(self) -> int:
        """How many slots are still empty — the *"còn bao nhiêu"* the ticket asks to show."""
        return len(self.pending)

    @property
    def answered(self) -> int:
        """How many slots hold something — the other half of the progress a creator reads.

        Here rather than as `total - remaining` at the call site, so the two
        numbers on the screen are counted off one sheet by one module. Progress
        arithmetic done in the HTTP layer is the one place a count could drift
        from the question being asked beside it.
        """
        return len(SLOT_SCHEDULE) - self.remaining

    @property
    def total(self) -> int:
        """How many slots the interview has in all — the denominator of the progress."""
        return len(SLOT_SCHEDULE)

    @property
    def is_complete(self) -> bool:
        """Whether every slot is filled, and the interview is therefore over.

        The deterministic stop, in one line and with no model in it. A creator
        whose description covered all five never sees a question at all, because
        this is already true before the first one would have been asked.
        """
        return not self.pending

    def value_of(self, slot: Slot) -> SlotValue | None:
        """What `slot` holds, or `None` if it is still empty."""
        return self.values.get(slot)

    def to_interview_slots(self) -> InterviewSlots:
        """This sheet as the `AgentSpec` field it exists to produce.

        Only valid on a complete sheet, and it says so rather than quietly
        building a half-empty one: `InterviewSlots` accepts empty lists (a spec
        written before the interview existed still has to validate), so an
        incomplete sheet would pass straight through and #17 would generate a spec
        against criteria nobody stated.
        """
        if not self.is_complete:
            missing = ", ".join(spec.slot.value for spec in self.pending)
            raise IncompleteSheetError(f"Phỏng vấn chưa xong, còn thiếu slot: {missing}")
        return InterviewSlots(
            audience=str(self.values[Slot.AUDIENCE]),
            inputs=str(self.values[Slot.INPUTS]),
            capabilities=list(self.values[Slot.CAPABILITIES]),
            success_criteria=list(self.values[Slot.SUCCESS_CRITERIA]),
            prohibitions=list(self.values[Slot.PROHIBITIONS]),
        )

    def _in_order(self) -> Iterable[tuple[Slot, SlotValue]]:
        """The filled slots in schedule order, so stored JSON reads like the interview."""
        return ((spec.slot, self.values[spec.slot]) for spec in SLOT_SCHEDULE if spec.slot in self.values)


class IncompleteSheetError(ValueError):
    """An unfinished interview was asked for the slots an `AgentSpec` needs."""


def _slot_named(name: str) -> Slot | None:
    """The `Slot` called `name`, or `None` if the schedule has no such slot."""
    try:
        return Slot(name)
    except ValueError:
        return None
