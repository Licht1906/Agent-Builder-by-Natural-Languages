"""`AgentSolutionSpecification` — what the business needs, before anything decides how.

V2's first structure (ADR-0020), and the whole output of the AI Solution Designer
interview (#42). Six of its seven fields are the creator's own sentences; the
spec names *what* the business wants and never *how* it will be executed. No node
type, no template id, no model name appears anywhere in here — which is what lets
the Workflow Generator (#43) be the first thing in the platform that knows any
workflow shape at all.

Two fields are structured, and both for the same kind of reason — something
downstream has to act on them mechanically:

- `data` carries a `ref`, because a creator can upload a file or connect an
  account *during* the interview, and a data source described but not addressed
  is a source the Workflow Generator cannot wire a node to.
- `success_criteria` carries a `category` from `SuccessCriterionCategory`, so a
  criterion routes straight to one of requirement §7's automatic checks without a
  second classification pass. "Calls a forbidden tool" is deliberately not one of
  them: that check reads `prohibitions` directly (ADR-0020 §1).

Every field refuses to be empty, and that is this module's other job. ADR-0020 §3
promises downstream structures a spec that is never partial — an
`AgentSolutionSpecification` only exists once the interview is finished — and a
promise enforced by a validator is one no caller can forget. The partial thing
the interview works on while it is still asking is `src.solution.draft.SpecDraft`,
which is a different type on purpose.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SuccessCriterionCategory(StrEnum):
    """Which automatic check grades a criterion — requirement §7's original six, plus ADR-0054 §4's two.

    Closed on purpose (ADR-0020, Consequences): a new evaluation dimension means
    extending this enum, not inventing a parallel classification beside it — the
    two ADR-0054 added are exactly that: `context_precision`/`task_completion`
    named a real gap in `src.evals.metrics.METRIC_CATALOG`'s RAGAS/DeepEval
    mapping, not a second taxonomy. The first six's split still runs deeper than
    a label — ADR-0028 routes the first three to rule-based readers of the run
    transcript and the rest to an LLM judge (`src.evals.judge`), and those two
    paths do not overlap.
    """

    WORKFLOW_SUCCESS = "workflow_success"
    TOOL_CORRECTNESS = "tool_correctness"
    HUMAN_APPROVAL_COMPLIANCE = "human_approval_compliance"
    SOURCE_GROUNDEDNESS = "source_groundedness"
    CORRECTNESS = "correctness"
    RELEVANCE = "relevance"
    #: ADR-0054 §4, the question RAGAS calls `context_precision`/`context_recall`,
    #: asked on this platform's one judge seam: did what the graph's retrieval
    #: step found — `KnowledgeSearch` passages or `WebSearch` results — contain
    #: what `expected` needed, without dragging in what it did not. Only the
    #: question is borrowed; the algorithm is a single judge call, so the number
    #: is not RAGAS's (see `src.evals.metrics`).
    CONTEXT_PRECISION = "context_precision"
    #: ADR-0054 §4, the question DeepEval calls `TaskCompletion`, asked the same
    #: way: did the whole transcript, not just the final sentence, actually
    #: finish what `input` asked for.
    TASK_COMPLETION = "task_completion"


class DataSourceKind(StrEnum):
    """Where a data source physically is, which decides whether it can be addressed.

    `uploaded_file`, `connected_account` and `database` name a resource that
    exists somewhere and therefore has a `ref`: a file the creator uploaded, an
    account they connected, a Hệ CSDL they registered (ADR-0059 §2). `web` and
    `user_input` name data that arrives at run time and has nothing to point at
    — see `DataSource.ref`.
    """

    UPLOADED_FILE = "uploaded_file"
    CONNECTED_ACCOUNT = "connected_account"
    WEB = "web"
    USER_INPUT = "user_input"
    DATABASE = "database"


#: The kinds that address a resource, and so are the only ones a `ref` makes
#: sense on. Derived from nothing — it is the distinction `DataSourceKind`
#: exists to draw, written once so the validator and the interview prompt agree.
ADDRESSABLE_DATA_KINDS = frozenset(
    {DataSourceKind.UPLOADED_FILE, DataSourceKind.CONNECTED_ACCOUNT, DataSourceKind.DATABASE}
)


def _non_blank(value: str) -> str:
    """`value` stripped, refusing anything that holds nothing.

    One rule for every free-text field in this module. A sentence of spaces is an
    unanswered question wearing a value, and it would otherwise satisfy every
    "this field is filled" check the platform makes about a confirmed spec.
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("không được để trống")
    return cleaned


def _non_blank_items(values: list[str]) -> list[str]:
    """Every entry stripped, refusing a list that turns out to hold nothing.

    Blank entries are dropped rather than refused one by one — a trailing empty
    line is a typing artifact, not a statement — but a list that is *only* blanks
    is refused, because it is indistinguishable from a field nobody answered.
    """
    cleaned = [item.strip() for item in values if item.strip()]
    if not cleaned:
        raise ValueError("không được để trống")
    return cleaned


class DataSource(BaseModel):
    """One place the agent's data comes from, and — when there is one — its address.

    `ref` is the single exception to ADR-0020's "the spec is business language"
    rule, and what it addresses depends on `kind`: for `uploaded_file` it is a
    Knowledge Base id, picked from the creator's own shelves on the spec screen
    or the canvas (ADR-0043 §1, §4) — never a single file, and never a blob key
    the way it was before the RAG uplift. For `connected_account` it is still a
    connection id, and for `database` the id of a Hệ CSDL (ADR-0059 §2). Either
    way it is filled in after the interview is over, by a person choosing
    something they actually attached; it stays `None` through the whole
    interview, because the model reading a description has no way to know which
    shelf, connection or database was meant, and a `ref` it invented
    would point at nothing (ADR-0025 §5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(..., description="Nguồn dữ liệu theo lời người tạo, ví dụ 'sổ tay xử lý khiếu nại'")
    kind: DataSourceKind
    ref: str | None = Field(
        default=None,
        description=(
            "Id Knowledge Base đã gắn (uploaded_file), id kết nối (connected_account) "
            "hoặc id Hệ CSDL (database); "
            "null khi chưa gắn tài nguyên thật nào"
        ),
    )

    _strip_name = field_validator("name")(_non_blank)

    @field_validator("ref")
    @classmethod
    def _blank_ref_is_no_ref(cls, ref: str | None) -> str | None:
        """A `ref` of whitespace is no reference at all, and is stored as none.

        Unlike the text fields this is not refused: a client clearing the box on
        the review screen is *saying* there is no resource yet, and answering that
        with a validation error would leave them no way to take a ref back.
        """
        return None if ref is None or not ref.strip() else ref.strip()

    @model_validator(mode="after")
    def _ref_needs_something_to_point_at(self) -> "DataSource":
        """Only an uploaded file, a connected account or a Hệ CSDL has an address.

        A `ref` on `web` or `user_input` would read on the review screen as a
        resource that was attached, and the Workflow Generator would wire a node
        to a key nothing can resolve. Refused here rather than ignored, because a
        silently dropped reference is the failure a creator cannot see.
        """
        if self.ref is not None and self.kind not in ADDRESSABLE_DATA_KINDS:
            raise ValueError(f"nguồn dữ liệu loại {self.kind.value!r} không trỏ tới tài nguyên nào nên không có 'ref'")
        return self


class SuccessCriterion(BaseModel):
    """One testable statement of "the agent did this right", and which check grades it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(..., description="Tiêu chí theo lời người tạo — eval sinh test case từ chính câu này")
    category: SuccessCriterionCategory = Field(..., description="Check tự động nào chấm tiêu chí này")

    _strip_text = field_validator("text")(_non_blank)


class AgentSolutionSpecification(BaseModel):
    """The confirmed business problem: seven fields, all of them answered.

    Frozen, and every field refused when empty. Together those two say the thing
    ADR-0020 §3 and §4 promise the rest of the platform: a spec that exists is
    complete, and a spec that is confirmed does not change afterwards. Editing the
    business intent means a new `AgentVersion`, never a write into this object.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    goal: str = Field(..., description="Bài toán nghiệp vụ cần giải, theo lời người tạo")
    users: list[str] = Field(..., min_length=1, description="Ai sẽ dùng agent này")
    data: list[DataSource] = Field(..., min_length=1, description="Agent đọc dữ liệu từ đâu")
    actions: list[str] = Field(..., min_length=1, description="Agent phải làm được những việc gì")
    expected_outcome: list[str] = Field(..., min_length=1, description="Chạy xong thì người dùng nhận được gì")
    success_criteria: list[SuccessCriterion] = Field(..., min_length=1, description="Thế nào là làm đúng")
    prohibitions: list[str] = Field(..., min_length=1, description="Tuyệt đối không được làm")

    _strip_goal = field_validator("goal")(_non_blank)
    _strip_lists = field_validator("users", "actions", "expected_outcome", "prohibitions")(_non_blank_items)
