"""Sample eval suites that ship with a template — DS-22 / ADR-0059 §11.

UC-1's golden set (`golden/uc1_ar_v3.jsonl`) was graded by its own runner, on
its own pipeline, against its own `principal`s. Both are gone (DS-21); what it
tested is not. `golden/cong_no_v1.jsonl` is the same 23 questions in the V2
suite schema (`src.evals.suite_io`), for the accounts-receivable templates:

- every case **asks as somebody** (`run_as`, DS-05) — one of `DEMO_ASKERS`,
  the employees the demo "ERP mẫu" seeds (DS-23);
- lookups, refusals, leaks and cross-scope reads are judged against an
  expectation that states the *property* — "only the asker's own customers",
  "no tax id" — never a count, so the suite stays true whatever the demo data
  holds on a given day;
- "act on this" and injected instructions are `takes_no_action` rules, graded
  off the transcript with no model in the loop. On `database_qa`, which has no
  action to decline, they are honestly skipped (ADR-0062 §3); on
  `database_outreach` they count;
- two more (EV-02) ask "khách nào quá hạn lâu nhất?" as the rep and as the lead
  and carry the query itself (`answers_from_query`): the answer key is that
  query on each asker's frozen `v_ar_open`, so the rep's and the lead's differ
  and neither is written by a model.

A creator loads it into an agent built from one of those templates with one
click (`POST /agents/{id}/eval-suite/sample`); it lands as an ordinary
uploaded suite, editable like any other.
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from src.db.models import Agent
from src.models.workflow import WorkflowJSON
from src.workflow.generator import WorkflowStageError, workflow_of

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "golden"

#: Who the sample suite's cases ask as — the demo employee view's `email` column (DS-23 seeds them).
DEMO_ASKERS = {
    "nhan_vien_kd": "nv.kinhdoanh@erp-mau.local",
    "nhan_vien_kd_2": "nv.kinhdoanh2@erp-mau.local",
    "truong_nhom_kd": "truongnhom.kd@erp-mau.local",
}


@dataclass(frozen=True)
class SampleSuite:
    """One shipped suite: what to call it and where its file is."""

    name: str
    path: Path

    def content(self) -> bytes:
        return self.path.read_bytes()


_CONG_NO = SampleSuite(name="Bộ đề mẫu: trợ lý công nợ", path=GOLDEN_DIR / "cong_no_v1.jsonl")

#: Template id → its sample suite. A template not listed here ships none.
SAMPLE_SUITES: dict[str, SampleSuite] = {
    "database_qa": _CONG_NO,
    "database_outreach": _CONG_NO,
}


def sample_suite_for(workflow: WorkflowJSON | None) -> SampleSuite | None:
    """The sample suite of the template `workflow` was generated from, or `None`."""
    if workflow is None or workflow.metadata.template_id is None:
        return None
    return SAMPLE_SUITES.get(workflow.metadata.template_id)


def sample_suite_of_agent(db: Session, agent: Agent) -> SampleSuite | None:
    """The sample suite of `agent`'s latest version's template — the version a suite import grounds against too."""
    from src.agents.versions import list_versions  # local import: suite_io's reason (a versions<->cases cycle)

    versions = list_versions(db, agent)
    if not versions:
        return None
    try:
        return sample_suite_for(workflow_of(versions[-1]))
    except WorkflowStageError:
        return None


def case_count(suite: SampleSuite) -> int:
    return sum(1 for line in suite.content().decode("utf-8").splitlines() if line.strip())
