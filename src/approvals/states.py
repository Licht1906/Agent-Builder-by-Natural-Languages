"""Why a version is on the approval queue.

Its own module, and the reason is an import direction rather than a design
preference. `src.approvals.queue` computes this value and `src.models.schemas`
puts it on the wire, but the queue reaches `src.evals.gate` for the score, which
reaches `src.evals.results`, which imports `src.models.schemas` for
`EvalCaseResult`. Declaring the enum in `queue` and importing it into `schemas`
would close that loop.

So it lives here, below all of them, and both sides name the same thing. The
alternative — a `Literal[...]` on the schema — would be two spellings of one set
of states, which is how a screen ends up rendering a state the backend stopped
producing.
"""

from enum import StrEnum


class QueueState(StrEnum):
    """The two ways a version can be waiting on an admin.

    They are separate because they call for different actions.
    `AWAITING_DECISION` wants a yes or a no. `BLOCKED_BY_EVAL` wants a judgement
    about whether the machine's verdict deserves to be waived — the version is
    not asking to be approved yet, and it cannot until someone opens that gate.

    One undifferentiated list would invite an admin to approve something that has
    not been submitted, which `src.agents.versions.approve` refuses anyway; the
    distinction is what keeps the screen from offering it.
    """

    AWAITING_DECISION = "awaiting_decision"
    BLOCKED_BY_EVAL = "blocked_by_eval"
