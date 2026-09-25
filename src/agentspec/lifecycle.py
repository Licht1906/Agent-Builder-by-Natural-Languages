"""The version lifecycle state machine (§13): which `SpecStatus` moves are legal,
and what has to be true before the platform makes one.

One named function per edge in the diagram — `submit_for_approval`, `approve`,
`deploy`, `suspend` — rather than a generic `transition(current, target, facts)`.
ADR-0047 made the same call for `validate_spec`/`validate_drafted_spec`: a flag
that picks between two policies is a keyword argument a caller can pass wrong: a
typo, not a wrong-looking call. Four names, one apiece, cannot be passed wrong in
that way.

Every function raises the same `InvalidTransitionError`, never a type of exception
per rule. `submit_for_approval` is where this earns its keep: §13 says of its three
conditions "thiếu bất kỳ điều kiện nào thì ở lại draft, không có nhánh lỗi riêng"
(missing any one leaves it in draft, with no branch of its own) — read here as "one
shape of refusal", not "no refusal at all", so a caller renders one checklist
instead of catching three different exception types.

`suspended -> retired` is not modeled. The flow diagram at the top of §13 draws
it, but the transition table underneath gives every other edge a condition and
skips this one, and no FR names one either. Guessing a condition here would be a
rule the spec never stated — the same call ADR-0047 made about
`confidence_threshold` not being baseline.

Editing a spec is not an edge in this table at all. `any -> draft (v+1)` creates a
new version rather than moving the one that exists — `new_draft_version` bumps
`metadata.version` and leaves the version it was called on untouched, which is
what makes "a deployed spec is never edited in place" true by construction.
"""

from enum import StrEnum

from src.agentspec.spec import AgentSpec, Evaluation, SpecStatus
from src.agentspec.validation import Problem


class SuspensionReason(StrEnum):
    """Why `deployed -> suspended` fired (§13). Structured for the same reason
    `Condition` is `{field, op, value}` (§7.1): the audit log records a reason,
    never a free-form string an admin typed.
    """

    ADMIN_KILL_SWITCH = "admin_kill_switch"
    REPEATED_POLICY_VIOLATION = "repeated_policy_violation"
    ROLLED_BACK = "rolled_back"
    #: Retired because a newer version of the same agent was deployed over it.
    #: Distinct from `ROLLED_BACK`, which is the same mechanical move made for
    #: the opposite reason — going *back* to an older version — and an audit log
    #: that could not tell the two apart would describe a routine upgrade and an
    #: emergency reversion with one word.
    SUPERSEDED = "superseded"


class InvalidTransitionError(Exception):
    """A lifecycle move was refused — its current status forbids it outright, or
    forbids it until a condition tied to that move holds. Carries every `Problem`
    found, mirroring `SpecValidationError`: a caller reads `.problems`, not a type.
    """

    def __init__(self, problems: list[Problem]) -> None:
        self.problems = problems
        super().__init__("; ".join(f"{problem.path}: {problem.message}" for problem in problems))


def submit_for_approval(
    spec: AgentSpec,
    *,
    form_confirmed: bool,
    sandbox_run_count: int,
    deepeval_pass_rate: float,
) -> AgentSpec:
    """`draft -> pending_approval` (§13).

    Three conditions gate this single move together: the form was confirmed
    (HITL #1), at least one sandbox run happened (FR-38), and the golden-set
    smoke test cleared the spec's own `evaluation.min_pass_rate` (FR-33,
    defaulting to `Evaluation`'s own value when the spec carries no `evaluation`
    block at all — see that field for why it currently sits below spec.md's 0.9).
    Missing more than one is reported in the same call rather than one at a time.

    `deepeval_pass_rate` is the golden set's own rate, produced by
    `src.evals.uc1_deepeval_runner` — a DeepEval-*style* single-call judge on
    this platform's model seam, not the `deepeval` package (that module's
    docstring says what the difference is and why the number is not comparable
    to a published DeepEval score). The parameter and the
    `deepeval_below_threshold` code keep the name because they are a wire
    contract the frontend and stored `Problem` payloads already match on;
    renaming them would break readers to correct a word.
    """
    if spec.metadata.status is not SpecStatus.DRAFT:
        raise InvalidTransitionError([_wrong_status(spec, SpecStatus.DRAFT, "submit-for-approval")])

    problems: list[Problem] = []
    if not form_confirmed:
        problems.append(
            Problem(code="form_not_confirmed", path="metadata.status", message="chưa xác nhận form (HITL #1)")
        )
    if sandbox_run_count < 1:
        problems.append(
            Problem(
                code="sandbox_run_missing",
                path="metadata.status",
                message="chưa có lượt chạy sandbox nào (FR-38)",
            )
        )
    min_pass_rate = spec.evaluation.min_pass_rate if spec.evaluation is not None else Evaluation().min_pass_rate
    if deepeval_pass_rate < min_pass_rate:
        problems.append(
            Problem(
                code="deepeval_below_threshold",
                path="metadata.status",
                message=(
                    f"Golden set đạt {deepeval_pass_rate:.2%}, chưa tới min_pass_rate "
                    f"{min_pass_rate:.2%} (FR-33)"
                ),
            )
        )
    if problems:
        raise InvalidTransitionError(problems)
    return _with_status(spec, SpecStatus.PENDING_APPROVAL)


def approve(spec: AgentSpec, *, approver_ids: list[str]) -> AgentSpec:
    """`pending_approval -> approved` (§13): enough distinct signatures to clear `authority.approval_quorum` (HITL #2).

    Writes `metadata.approved_by` — the field `Metadata`'s own docstring (#111)
    names as existing for exactly this check — rather than taking a bare count
    that could never be written back. Duplicate ids collapse to one signer before
    counting: a quorum is a count of *people*, and the same id twice is one
    signature, not two.
    """
    if spec.metadata.status is not SpecStatus.PENDING_APPROVAL:
        raise InvalidTransitionError([_wrong_status(spec, SpecStatus.PENDING_APPROVAL, "approve")])

    distinct_approvers = list(dict.fromkeys(approver_ids))
    quorum = spec.authority.approval_quorum
    if len(distinct_approvers) < quorum:
        raise InvalidTransitionError(
            [
                Problem(
                    code="approval_quorum_not_met",
                    path="metadata.status",
                    message=f"cần {quorum} chữ ký (authority.approval_quorum), mới có {len(distinct_approvers)}",
                )
            ]
        )
    return spec.model_copy(
        update={
            "metadata": spec.metadata.model_copy(
                update={"status": SpecStatus.APPROVED, "approved_by": distinct_approvers}
            )
        }
    )


def deploy(spec: AgentSpec) -> AgentSpec:
    """`approved -> deployed` (§13). Only the status gates this move — provisioning
    the service account and PG role (FR-34) is the compiler's job, not this
    module's.
    """
    if spec.metadata.status is not SpecStatus.APPROVED:
        raise InvalidTransitionError([_wrong_status(spec, SpecStatus.APPROVED, "deploy")])
    return _with_status(spec, SpecStatus.DEPLOYED)


#: UC1-25 (#141), FR-51: the two statuses AC-25's own "target version có status
#: `approved`/từng `deployed`" names — `approved` never having been deployed at
#: all, `suspended` having been deployed once and taken down since (the only
#: state `deployed` can leave to, per this table).
_ROLLBACK_ELIGIBLE_STATUSES = frozenset({SpecStatus.APPROVED, SpecStatus.SUSPENDED})


def rollback(spec: AgentSpec) -> AgentSpec:
    """`approved|suspended -> deployed` (§13, FR-51): point `deployed_version`
    back at a version that already cleared HITL #2 once — never a new version,
    never a fresh signature. Distinct from `deploy` (whose *only* legal
    starting status is `approved`, the normal forward path) because a rollback
    target may equally be `suspended` — a version that was deployed before and
    is being brought back, not promoted for the first time.
    """
    if spec.metadata.status not in _ROLLBACK_ELIGIBLE_STATUSES:
        raise InvalidTransitionError(
            [
                Problem(
                    code="invalid_transition",
                    path="metadata.status",
                    message=(
                        f"rollback chỉ hợp lệ từ 'approved' hoặc 'suspended', "
                        f"version này đang ở {spec.metadata.status.value!r}"
                    ),
                )
            ]
        )
    return _with_status(spec, SpecStatus.DEPLOYED)


def suspend(spec: AgentSpec, *, reason: SuspensionReason) -> AgentSpec:
    """`deployed -> suspended` (§13): an admin's kill switch, or a repeated policy violation.

    `reason` gates nothing here — the audit log is where it belongs (§14), and this
    signature exists so no caller can suspend a spec without naming one.
    """
    if spec.metadata.status is not SpecStatus.DEPLOYED:
        raise InvalidTransitionError([_wrong_status(spec, SpecStatus.DEPLOYED, "suspend")])
    return _with_status(spec, SpecStatus.SUSPENDED)


def new_draft_version(spec: AgentSpec) -> AgentSpec:
    """`any -> draft (v+1)` (§13): allocate the next version rather than edit this one.

    Valid from every status, `deployed` included — that row is the one AC2 names:
    editing a deployed spec always produces a new draft, never a mutation of the
    running version. `spec` itself comes back unchanged; the new version is a
    fresh `AgentSpec`, `parent_version` pointing at the one it was called on and
    `approved_by` reset, because nobody has signed a version that does not exist
    yet.
    """
    new_metadata = spec.metadata.model_copy(
        update={
            "version": spec.metadata.version + 1,
            "status": SpecStatus.DRAFT,
            "parent_version": spec.metadata.version,
            "approved_by": [],
        }
    )
    return spec.model_copy(update={"metadata": new_metadata})


def _with_status(spec: AgentSpec, status: SpecStatus) -> AgentSpec:
    return spec.model_copy(update={"metadata": spec.metadata.model_copy(update={"status": status})})


def _wrong_status(spec: AgentSpec, required: SpecStatus, operation: str) -> Problem:
    return Problem(
        code="invalid_transition",
        path="metadata.status",
        message=(
            f"{operation} chỉ hợp lệ từ trạng thái '{required.value}', "
            f"version này đang ở '{spec.metadata.status.value}'"
        ),
    )
