"""The half-filled spec an interview works on, and what counts as an answered field.

`AgentSolutionSpecification` refuses to be partial (ADR-0020 §3), so the thing
the interview holds between turns has to be a different type. This is it: the
same seven fields, every one of them allowed to be empty, plus the one rule that
decides everything the session does with them — **a field is answered when it
holds something usable**. Which question comes next, whether the interview may
end, and whether a spec can be built at all are all read off that.

It is deliberately forgiving on the way in and strict on the way out. `read`
takes a document from a model or a database row and keeps whatever it can use,
dropping the rest without complaint — a model that answered `users` with a number
has answered nothing, and the interview simply asks. `to_specification` is the
opposite: it hands over an `AgentSolutionSpecification` or it names the fields
that are still empty, so nothing downstream ever receives a spec with a hole in
it.

The order of `FIELD_SCHEDULE` is the order a creator is asked, and it is
ADR-0020's own order: what a description almost always states (the goal, who it
is for) first, what it almost never states (what "correct" means, what is
forbidden) last. A creator who abandons the interview part-way has answered the
questions they were most able to answer.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import ValidationError

from src.models.solution_spec import (
    AgentSolutionSpecification,
    DataSource,
    DataSourceKind,
    SuccessCriterion,
    SuccessCriterionCategory,
)

#: The two structured entries `read` reconstructs from a document, so the one
#: tolerant validator below can serve both without either losing its own type.
_Structured = TypeVar("_Structured", DataSource, SuccessCriterion)


class SpecField(StrEnum):
    """The seven fields of an Agent Solution Specification, in interview order."""

    GOAL = "goal"
    USERS = "users"
    DATA = "data"
    ACTIONS = "actions"
    EXPECTED_OUTCOME = "expected_outcome"
    SUCCESS_CRITERIA = "success_criteria"
    PROHIBITIONS = "prohibitions"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One field: what it is called, what the creator is asked, and what a good answer is.

    `question` is the sentence a creator sees when the model does not write a
    better one of its own, so it is also the sentence the model is shown in the
    prompt. One copy of it, for the reason V1 gives (`src.interview.slots`): a
    model filling "expected_outcome" and a creator answering "chạy xong thì bạn
    nhận được gì?" must be working from the same question.
    """

    field: SpecField
    question: str
    hint: str
    """What a usable answer looks like. Shown to the model, not to the creator."""


#: The interview, in full. The prompt, the fallback questions and the "what is
#: still missing" count are all read off this tuple, so none of them can fall out
#: of step with the others.
FIELD_SCHEDULE: tuple[FieldSpec, ...] = (
    FieldSpec(
        field=SpecField.GOAL,
        question="Bài toán nghiệp vụ bạn muốn agent này giải là gì?",
        hint="một câu nói rõ kết quả nghiệp vụ mong muốn, không phải cách làm",
    ),
    FieldSpec(
        field=SpecField.USERS,
        question="Ai sẽ dùng agent này?",
        hint="từng nhóm người một, ví dụ 'nhân viên chăm sóc khách hàng phòng kinh doanh'",
    ),
    FieldSpec(
        field=SpecField.DATA,
        # Names the four kinds to the *creator*, not only to the model. This is
        # one of the two fields with no raw-words fallback (`with_answer`), so it
        # is one of the two that can be asked twice — and a creator who is told
        # what shape of answer is missing is the thing that stops it being asked
        # a third time.
        question=(
            "Agent cần đọc dữ liệu từ đâu? Với mỗi nguồn, cho biết nó là file bạn tải lên, "
            "tài khoản bạn kết nối (Gmail/Drive/...), cơ sở dữ liệu của công ty (ERP, CRM...), "
            "thông tin tìm trên web, hay dữ liệu người dùng nhập lúc chạy."
        ),
        hint=(
            "từng nguồn một, mỗi nguồn kèm loại: 'uploaded_file' (file người tạo tải lên), "
            "'connected_account' (tài khoản Gmail/Drive/... đã kết nối), 'database' (dữ liệu nghiệp vụ có "
            "cấu trúc trong ERP/CRM/CSDL của công ty — công nợ, đơn hàng, tồn kho...), 'web' (tìm trên web), "
            "'user_input' (dữ liệu người dùng nhập lúc chạy)"
        ),
    ),
    FieldSpec(
        field=SpecField.ACTIONS,
        question="Agent cần làm được những việc gì?",
        hint="từng việc một, nói bằng ngôn ngữ nghiệp vụ chứ không phải tên tool hay tên bước",
    ),
    FieldSpec(
        field=SpecField.EXPECTED_OUTCOME,
        question="Chạy xong thì người dùng nhận được gì?",
        hint="từng kết quả cụ thể một, ví dụ 'một thư trả lời đã soạn sẵn chờ nhân viên gửi'",
    ),
    FieldSpec(
        field=SpecField.SUCCESS_CRITERIA,
        # The other field with no raw-words fallback, and the same remedy: a
        # criterion is only usable once somebody has said which check grades it,
        # so the creator is asked in terms of the six checks rather than being
        # asked twice for a sentence that keeps arriving unclassified.
        question=(
            "Thế nào là agent này làm đúng? Với mỗi tiêu chí, cho biết nó kiểm điều gì: "
            "chạy xong đúng quy trình, gọi đúng công cụ, tuân thủ quyết định của người duyệt, "
            "bám đúng nguồn tài liệu, trả lời chính xác, hay trả lời đúng trọng tâm."
        ),
        hint=(
            "từng tiêu chí kiểm được một, mỗi tiêu chí kèm 'category' là một trong sáu giá trị: "
            "workflow_success, tool_correctness, human_approval_compliance, source_groundedness, "
            "correctness, relevance"
        ),
    ),
    FieldSpec(
        field=SpecField.PROHIBITIONS,
        question="Agent tuyệt đối không được làm những gì?",
        hint="từng điều cấm một; nếu nghiệp vụ không cấm gì thì nói rõ là không có",
    ),
)


class IncompleteDraftError(ValueError):
    """A draft with empty fields was asked for the specification it is not yet."""


@dataclass(frozen=True, slots=True)
class SpecDraft:
    """What the interview knows so far. Empty fields are the questions still to ask.

    Immutable, and the two ways to change it — `merged_with` for what a model
    read, `with_answer` for the creator's own words — both return a new draft. A
    session's whole state is then the draft it holds plus the conflicts flagged
    against it, which is what lets `sessions` persist a JSON column and re-derive
    the rest.
    """

    goal: str = ""
    users: tuple[str, ...] = ()
    data: tuple[DataSource, ...] = ()
    actions: tuple[str, ...] = ()
    expected_outcome: tuple[str, ...] = ()
    success_criteria: tuple[SuccessCriterion, ...] = ()
    prohibitions: tuple[str, ...] = ()

    @classmethod
    def empty(cls) -> "SpecDraft":
        """A draft before anything has been read into it — seven fields to go."""
        return cls()

    @classmethod
    def read(cls, document: Mapping[str, Any]) -> "SpecDraft":
        """Whatever `document` usably says about the seven fields.

        The single reader of untrusted JSON in this package: a model's reply and
        the session row that reply produced both arrive here, so "usable" cannot
        mean two different things depending on which side of a save it is read
        on. Anything it cannot use is dropped rather than coerced — `str()`
        around whatever a model returned is how `"{'a': 1}"` ends up stored as a
        business goal, and a goal nobody can read is worse than a missing one
        because it still counts as answered.

        A `ref` in the document is dropped, whatever it says. A reference names a
        file that was uploaded or an account that was connected, and neither of
        those is something a model reading a description can know — see
        `DataSource.ref`.
        """
        return cls(
            goal=_text(document.get(SpecField.GOAL)),
            users=lines(document.get(SpecField.USERS)),
            data=_entries(document.get(SpecField.DATA), DataSource, ("name", "kind")),
            actions=lines(document.get(SpecField.ACTIONS)),
            expected_outcome=lines(document.get(SpecField.EXPECTED_OUTCOME)),
            success_criteria=_entries(document.get(SpecField.SUCCESS_CRITERIA), SuccessCriterion, ("text", "category")),
            prohibitions=lines(document.get(SpecField.PROHIBITIONS)),
        )

    def to_stored(self) -> dict[str, Any]:
        """This draft as JSON the session row can hold — only the fields that hold something.

        A field missing from the document *is* a field still to ask about, the
        same discipline V1's slot sheet uses: presence is filled-ness, so there is
        no second flag to keep in step with the value.
        """
        return {field.value: _stored_value(value) for field, value in self._filled()}

    def merged_with(self, reading: "SpecDraft") -> "SpecDraft":
        """This draft with every field `reading` actually filled written over it.

        A field `reading` says nothing about keeps what it already held. Every
        turn re-reads the whole conversation, so silence about a field is a model
        that had nothing to add — not a retraction of what the creator already
        said, which is how an interview would otherwise walk backwards on its
        third question.

        A field `reading` *does* fill is merged entry by entry (`_merged_field`):
        an entry with the same identity is replaced — that is the correction
        ADR-0020 §3 asks for, and the deliberate difference from V1
        (`SlotSheet.with_values`, which never overwrites) — and an entry the
        reading left out stays, because a reading that forgets a source is not
        a creator retracting one (ADR-0067 §4).
        """
        return SpecDraft(
            **{field.value: _merged_field(getattr(self, field.value), getattr(reading, field.value)) for field in SpecField}
        )

    def with_answer(self, field: SpecField, answer: str, *, force: bool = False) -> "SpecDraft":
        """This draft with the creator's own sentence stored in `field`.

        `force` is the exception to the exception below, and it exists because
        of a measured failure rather than a hypothetical one: on 2026-08-30 an
        interview asked *"file chính sách có định dạng gì?"* ten turns in a row
        while the creator kept answering. After `sessions.MAX_QUESTIONS_PER_FIELD`
        turns on one of the two structured fields, the creator's sentence is
        stored under the **least consequential** default — `user_input` creates
        no node at all, `correctness` routes to the judge that can grade any
        sentence — because an interview nobody can finish is worse than a spec
        the creator will see and can edit on the next screen.

        The fallback for a turn where the model read nothing out of what the
        creator just said. Their raw sentence is a worse-structured answer than
        the model's reading of it and a far better one than silence — and it is
        what guarantees an answered question does not come straight back.

        **The two structured fields are excepted** and return the draft
        untouched, for one reason said twice. A `DataSource` needs a `kind`, which
        decides how the Workflow Generator wires it up (ADR-0025); a
        `SuccessCriterion` needs a `category`, which decides which evaluator
        grades it (ADR-0028). Neither can be read off "hộp thư hỗ trợ" or "thư
        phải nêu đúng mã đơn", and a value guessed here does not stay here — it
        propagates into a workflow, or routes an approval-compliance criterion to
        an LLM judge that cannot see what it is about. Both are worse than asking
        again, which is why `_data_sources` and `_criteria` drop an unusable entry
        rather than filing it under a default, and why this does the same.

        The cost is that those two fields can be asked about more than once, so
        their questions in `FIELD_SCHEDULE` say out loud what shape of answer they
        need — see ADR-0030 §1.
        """
        text = answer.strip()
        if not text:
            return self
        match field:
            case SpecField.GOAL:
                return replace(self, goal=text)
            case SpecField.USERS:
                return replace(self, users=(text,))
            case SpecField.ACTIONS:
                return replace(self, actions=(text,))
            case SpecField.EXPECTED_OUTCOME:
                return replace(self, expected_outcome=(text,))
            case SpecField.PROHIBITIONS:
                return replace(self, prohibitions=(text,))
            case SpecField.DATA:
                if not force:
                    return self
                return replace(self, data=(DataSource(name=text, kind=DataSourceKind.USER_INPUT),))
            case SpecField.SUCCESS_CRITERIA:
                if not force:
                    return self
                return replace(
                    self,
                    success_criteria=(SuccessCriterion(text=text, category=SuccessCriterionCategory.CORRECTNESS),),
                )

    def with_criteria(self, sentences: Sequence[str]) -> "SpecDraft":
        """This draft with `sentences` appended as criteria the judge grades (`correctness`).

        The numeric guard's write path (`sessions._settled`): a creator who just
        said *"từ 60 ngày là thư cảnh báo"* and had that read back as *"chính xác"*
        gets their own sentence kept, number and all, beside whatever the model
        did read. Appended, never replacing — the model's reading may be right
        about the rest.
        """
        kept = {" ".join(criterion.text.split()) for criterion in self.success_criteria}
        added = tuple(
            SuccessCriterion(text=text, category=SuccessCriterionCategory.CORRECTNESS)
            for sentence in sentences
            if (text := " ".join(sentence.split())) and text not in kept
        )
        return replace(self, success_criteria=(*self.success_criteria, *added)) if added else self

    def value_of(self, field: SpecField) -> Any:
        """What `field` holds — `""` or `()` while it is still empty."""
        return getattr(self, field.value)

    def filled(self, field: SpecField) -> bool:
        """Whether `field` holds something, and is therefore not a question any more."""
        return bool(self.value_of(field))

    @property
    def missing(self) -> tuple[FieldSpec, ...]:
        """The fields still empty, in interview order — exactly what is left to ask about."""
        return tuple(spec for spec in FIELD_SCHEDULE if not self.filled(spec.field))

    @property
    def is_complete(self) -> bool:
        """Whether every field holds something, so a specification could be built."""
        return not self.missing

    def to_specification(self) -> AgentSolutionSpecification:
        """This draft as the immutable spec it exists to become.

        Refuses while anything is empty, naming the fields — the message reaches
        the creator as the reason their interview is not finished. Without the
        refusal `AgentSolutionSpecification`'s own validators would answer instead,
        in pydantic's English, about a document the creator has never seen.
        """
        if not self.is_complete:
            missing = ", ".join(spec.field.value for spec in self.missing)
            raise IncompleteDraftError(f"Đặc tả chưa đủ, còn thiếu: {missing}")
        return AgentSolutionSpecification(
            goal=self.goal,
            users=list(self.users),
            data=list(self.data),
            actions=list(self.actions),
            expected_outcome=list(self.expected_outcome),
            success_criteria=list(self.success_criteria),
            prohibitions=list(self.prohibitions),
        )

    def _filled(self) -> list[tuple[SpecField, Any]]:
        """The fields that hold something, in schedule order, so stored JSON reads like the interview."""
        return [(spec.field, self.value_of(spec.field)) for spec in FIELD_SCHEDULE if self.filled(spec.field)]


def _merged_field(known: Any, read: Any) -> Any:
    """One field after a reading: the reading's *additions and corrections*, never its omissions.

    `merged_with` used to let a non-empty reading replace the whole field, on
    ADR-0020 §3's argument that a later reading may correct an earlier one. It
    may — but measured on 2026-08-30 (`benchmark/pitch_kinh_doanh`, agent 2, runs
    23–24) what a later reading mostly did was *forget*: a pass that read the
    creator's criteria answer wrote `data` back with one source of three, and
    the Gmail account the whole agent existed to use was gone from the spec. So
    a list field is now a union keyed by the entry's identity — a source's name,
    a criterion's text, a sentence — where a re-read entry with the same key
    replaces the old one (that is the correction: *"không phải file, là tài
    khoản"* changes the kind under the same name) and an entry the reading did
    not mention stays. `goal`, a single sentence, is still replaced outright.
    """
    if not read:
        return known
    if isinstance(read, str) or not isinstance(known, tuple):
        return read
    merged: dict[str, Any] = {_identity(entry): entry for entry in known}
    for entry in read:
        merged[_identity(entry)] = entry
    return tuple(merged.values())


def _identity(entry: Any) -> str:
    """What makes two entries the same thing: a source's name, a criterion's text, a sentence."""
    from src.workflow.effects import without_diacritics

    if isinstance(entry, DataSource):
        text = entry.name
    elif isinstance(entry, SuccessCriterion):
        text = entry.text
    else:
        text = str(entry)
    return " ".join(without_diacritics(text).lower().split())


def _stored_value(value: Any) -> Any:
    """One field's value as JSON — a string stays one, a tuple becomes a list of plain documents."""
    if isinstance(value, str):
        return value
    return [
        item.model_dump(mode="json") if isinstance(item, (DataSource, SuccessCriterion)) else item for item in value
    ]


def _text(raw: object) -> str:
    """`raw` as a single stripped sentence, or `""` if it is not usable text."""
    return raw.strip() if isinstance(raw, str) else ""


def lines(raw: object) -> tuple[str, ...]:
    """Every non-empty string in `raw`, stripped — accepting a bare string as one item.

    Forgiving about shape for the reason V1's `normalise` is: a model that
    answered a list field with one sentence meant one item, and refusing it would
    cost the creator a question over a formatting habit.

    Public because `designer` reads the model's `conflicts` the same way, and the
    same forgiveness has to apply: a model that flagged one contradiction as a
    bare string flagged one contradiction.
    """
    values = raw if isinstance(raw, list) else [raw]
    return tuple(item.strip() for item in values if isinstance(item, str) and item.strip())


def _entries(raw: object, model: type[_Structured], keys: tuple[str, ...]) -> tuple[_Structured, ...]:
    """The entries in `raw` that are usable `model`s, built from `keys` and nothing else.

    Both structured fields read the same way, so they read through one function.
    Each entry is rebuilt key by key rather than handed straight to the
    validator: `DataSource` and `SuccessCriterion` both forbid extra fields, so a
    model that helpfully added a `description` would otherwise cost the whole
    list rather than the one key nobody asked for. It is also what drops a `ref`
    the model invented, since `ref` is not among the keys either field asks for.

    An entry that still does not validate is dropped whole rather than repaired.
    Both fields carry a value the platform *routes* on — `kind` decides how the
    Workflow Generator wires a source (ADR-0025), `category` decides which
    evaluator grades a criterion (ADR-0028) — and a wrong one is worse than a
    missing one, because a missing one is a question the interview asks again.
    """
    entries = (_validated(model, _normalised({key: entry.get(key) for key in keys})) for entry in _documents(raw))
    return tuple(entry for entry in entries if entry is not None)


#: What a model calls a category when it does not use the enum's exact spelling,
#: mapped to the spelling. Measured 2026-08-30 (`benchmark/pitch_kinh_doanh`, agent
#: 2, runs 20–24): a four-criterion answer that named every check in the creator's
#: words was read back as *zero* criteria four times out of five, because each
#: entry's `category` failed validation and was dropped whole — and a field that
#: still held its one old criterion counted as "filled", so nothing asked again.
#: Unknown categories fall to `correctness`, the judge that grades any sentence
#: (the same default `with_answer(force=True)` uses).
_CATEGORY_SYNONYMS: dict[str, str] = {
    "approval": "human_approval_compliance",
    "human_approval": "human_approval_compliance",
    "approval_compliance": "human_approval_compliance",
    "tuan_thu_nguoi_duyet": "human_approval_compliance",
    "tool": "tool_correctness",
    "tool_call": "tool_correctness",
    "tools": "tool_correctness",
    "goi_dung_cong_cu": "tool_correctness",
    "groundedness": "source_groundedness",
    "grounded": "source_groundedness",
    "source": "source_groundedness",
    "sources": "source_groundedness",
    "bam_nguon": "source_groundedness",
    "workflow": "workflow_success",
    "process": "workflow_success",
    "dung_quy_trinh": "workflow_success",
    "relevant": "relevance",
    "trong_tam": "relevance",
    "correct": "correctness",
    "accuracy": "correctness",
    "accurate": "correctness",
    "chinh_xac": "correctness",
    "precision": "context_precision",
    "context": "context_precision",
    "completion": "task_completion",
    "task": "task_completion",
}

#: The same for `kind`, which is *not* defaulted: a kind routes a node, and an
#: unplaceable source is better re-asked than wired wrong.
_KIND_SYNONYMS: dict[str, str] = {
    "file": "uploaded_file",
    "upload": "uploaded_file",
    "uploaded": "uploaded_file",
    "document": "uploaded_file",
    "knowledge_base": "uploaded_file",
    "account": "connected_account",
    "connected": "connected_account",
    "connection": "connected_account",
    "gmail": "connected_account",
    "drive": "connected_account",
    "internet": "web",
    "web_search": "web",
    "input": "user_input",
    "user": "user_input",
    "chat": "user_input",
    "runtime": "user_input",
    # DS-11: a Hệ CSDL, however a model spells it.
    "db": "database",
    "sql": "database",
    "erp": "database",
    "crm": "database",
    "csdl": "database",
    "he_csdl": "database",
    "co_so_du_lieu": "database",
    "table": "database",
    "database_table": "database",
}


def _normalised(document: dict[str, Any]) -> dict[str, Any]:
    """`document` with a `category`/`kind` written in the model's own words moved to the enum's."""
    from src.workflow.effects import without_diacritics

    out = dict(document)
    for key, table, default in (("category", _CATEGORY_SYNONYMS, "correctness"), ("kind", _KIND_SYNONYMS, None)):
        if key not in out or not isinstance(out[key], str):
            continue
        raw = without_diacritics(out[key]).strip().lower().replace("-", "_").replace(" ", "_")
        valid = {member.value for member in (SuccessCriterionCategory if key == "category" else DataSourceKind)}
        if raw in valid:
            out[key] = raw
        elif raw in table:
            out[key] = table[raw]
        elif default is not None:
            out[key] = default
    return out


def _documents(raw: object) -> list[Mapping[str, Any]]:
    """Every mapping inside `raw` — a bare mapping counts as a list of one."""
    values: Sequence[Any] = raw if isinstance(raw, list) else [raw]
    return [entry for entry in values if isinstance(entry, Mapping)]


def _validated(model: type[_Structured], document: Mapping[str, Any]) -> _Structured | None:
    """`document` as a `model`, or `None` if it is not one.

    The single place a structured entry is allowed to fail quietly. Whatever the
    model got wrong about one entry, the interview's answer is the same: that
    entry is not something we know, so the field either has other entries or is
    still a question.
    """
    try:
        return model.model_validate(document)  # type: ignore[attr-defined,no-any-return]
    except ValidationError:
        return None


def field_named(raw: object) -> SpecField | None:
    """The `SpecField` called `raw`, or `None` if the spec has no field of that name.

    Beside `SpecField` because both readers of an untrusted field name — a
    model's reply (`designer`) and a stored open question (`sessions`) — have to
    agree about which names exist, and two copies of the lookup is how they would
    stop agreeing the day a field is renamed.
    """
    if not isinstance(raw, str):
        return None
    try:
        return SpecField(raw)
    except ValueError:
        return None
