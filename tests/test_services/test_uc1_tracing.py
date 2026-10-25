"""UC1-31 (#143) — `src.services.uc1_tracing`.

`log_decision` is tested against Python's own `logging` (via `caplog`), the
same seam a real log shipper reads. `export_uc1_trace` is tested against a
fake Langfuse client with the same `start_observation`/`update`/`end` shape
`src.services.tracing.LangfuseTrace` already assumes for V2 — no live
Langfuse, matching this repo's model-factory seam principle (§20).
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.agentspec.spec import AgentSpec, Observability, Trace
from src.compiler.state import TranscriptStep
from src.policy.pep import Action, AuthzRequest, Decision, Effect, Principal, Provenance, Session
from src.services import uc1_tracing
from tests.test_agentspec.conftest import spec_payload


@pytest.fixture(autouse=True)
def _undo_alembics_disable_existing_loggers() -> None:
    """`alembic/env.py`'s own `fileConfig(config.config_file_name)` — run by
    every `command.upgrade` call, including `tests/test_db`'s own
    `upgrade_to_head` — defaults to `disable_existing_loggers=True`, which
    sets `.disabled = True` on every already-created logger `alembic.ini`'s
    `[loggers]` section does not name (only `root`/`sqlalchemy`/`alembic`
    are). `uc1.audit` (`log_decision`, below) is one of those, and a
    `Logger` object is a process-wide singleton keyed by name — so once any
    test anywhere in this session runs a real migration, this logger stays
    disabled for the rest of the process, silently, regardless of
    `caplog.at_level`'s own `setLevel` call (which does not touch
    `.disabled`). Not `alembic/env.py`'s or `alembic.ini`'s bug to fix here —
    this module's own test is simply the first in the suite to assert on
    captured output from a non-alembic logger.
    """
    logging.getLogger("uc1.audit").disabled = False
    logging.getLogger("uc1.observability").disabled = False


def _spec(**overrides) -> AgentSpec:
    return AgentSpec.model_validate(spec_payload(**overrides))


def _request(spec: AgentSpec) -> AuthzRequest:
    return AuthzRequest(
        spec=spec,
        principal=Principal(sub="u1", employee_id="E102", roles=["sales_rep"], department="SALES"),
        action=Action(hash="h1", tool="erp.query_receivables", scope=spec.capabilities[0].scope, mode="execute", connection_id="", args={}),
        provenance=Provenance(from_untrusted_data=False),
        session=Session(tool_calls_used=0),
    )


def _decision(effect: Effect = Effect.ALLOW) -> Decision:
    return Decision(effect=effect, reasons=[], row_filter={}, approver_roles=[], redact_fields=[], max_rows=100)


def _step(*, node: str = "tools", tool: str | None = "erp.query_receivables", args: dict | None = None, result: str | None = "ok") -> TranscriptStep:
    return {
        "node": node,
        "tool": tool,
        "args": args if args is not None else {},
        "result": result,
        "tokens": 12,
        "latency_ms": 3.4,
        "cost_usd": 0.002,
        "row_filter": {},
    }


# --------------------------------------------------------------------------- #
# log_decision
# --------------------------------------------------------------------------- #


def test_log_decision_emits_a_json_line_with_the_five_documented_fields(caplog: pytest.LogCaptureFixture) -> None:
    spec = _spec()
    with caplog.at_level(logging.INFO, logger="uc1.audit"):
        uc1_tracing.log_decision("req-1", _request(spec), _decision(Effect.DENY))

    assert len(caplog.records) == 1
    payload = json.loads(caplog.records[0].getMessage())
    assert payload == {
        "request_id": "req-1",
        "agent_id": "agt_ar_assistant",
        "principal_id": "E102",
        "tool": "erp.query_receivables",
        "effect": "deny",
    }


# --------------------------------------------------------------------------- #
# export_uc1_trace — fake Langfuse client
# --------------------------------------------------------------------------- #


@dataclass
class _FakeObservation:
    name: str
    as_type: str
    input: Any = None
    output: Any = None
    metadata: dict = field(default_factory=dict)
    ended: bool = False
    children: list["_FakeObservation"] = field(default_factory=list)

    def start_observation(self, *, name: str, as_type: str, input: Any = None, metadata: dict | None = None) -> "_FakeObservation":  # noqa: A002
        child = _FakeObservation(name=name, as_type=as_type, input=input, metadata=dict(metadata or {}))
        self.children.append(child)
        return child

    def update(self, *, output: Any = None, metadata: dict | None = None, level: str | None = None) -> None:
        self.output = output
        self.metadata.update(metadata or {})

    def end(self) -> None:
        self.ended = True


class _FakeLangfuseClient:
    def __init__(self) -> None:
        self.roots: list[_FakeObservation] = []
        #: The `trace_id` each root was filed under — ADR-0053's cross-link to
        #: `agent_runs` is only real if this is the turn's own `request_id`.
        self.trace_ids: list[str | None] = []

    def start_observation(
        self, *, name: str, as_type: str, metadata: dict | None = None, trace_context: Any = None
    ) -> _FakeObservation:
        root = _FakeObservation(name=name, as_type=as_type, metadata=dict(metadata or {}))
        self.roots.append(root)
        self.trace_ids.append(dict(trace_context).get("trace_id") if trace_context else None)
        return root


@pytest.fixture
def fake_langfuse(monkeypatch: pytest.MonkeyPatch) -> _FakeLangfuseClient:
    client = _FakeLangfuseClient()
    monkeypatch.setattr(uc1_tracing, "_langfuse_client", lambda: client)
    return client


def _traced_spec(*, pii_scrub_traces: bool = True) -> AgentSpec:
    spec = _spec()
    return spec.model_copy(
        update={"observability": Observability(trace=Trace.LANGFUSE, pii_scrub_traces=pii_scrub_traces)}
    )


def test_no_trace_is_opened_when_spec_observability_is_unset(fake_langfuse: _FakeLangfuseClient) -> None:
    uc1_tracing.export_uc1_trace(
        [_step()], spec=_spec(), principal=Principal(sub="u1", employee_id="E102", roles=[], department=""), thread_id="t1", request_id="r1"
    )
    assert fake_langfuse.roots == []


def test_no_trace_is_opened_when_trace_is_explicitly_none(fake_langfuse: _FakeLangfuseClient) -> None:
    spec = _spec().model_copy(update={"observability": Observability(trace=Trace.NONE)})
    uc1_tracing.export_uc1_trace(
        [_step()], spec=spec, principal=Principal(sub="u1", employee_id="E102", roles=[], department=""), thread_id="t1", request_id="r1"
    )
    assert fake_langfuse.roots == []


def test_no_trace_is_opened_when_langfuse_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """`spec` opts in, but `_langfuse_client()` itself returns `None` (no keys) — still nothing exported."""
    monkeypatch.setattr(uc1_tracing, "_langfuse_client", lambda: None)
    uc1_tracing.export_uc1_trace(
        [_step()],
        spec=_traced_spec(),
        principal=Principal(sub="u1", employee_id="E102", roles=[], department=""),
        thread_id="t1",
        request_id="r1",
    )  # must not raise


def test_a_trace_carries_one_child_observation_per_transcript_step(fake_langfuse: _FakeLangfuseClient) -> None:
    steps = [_step(node="agent", tool=None, args=None, result="xin chào"), _step(node="tools", tool="erp.query_receivables", args={"days_overdue_gt": 30}, result="7 rows")]

    uc1_tracing.export_uc1_trace(
        steps, spec=_traced_spec(), principal=Principal(sub="u1", employee_id="E102", roles=[], department=""), thread_id="thread-1", request_id="req-9"
    )

    assert len(fake_langfuse.roots) == 1
    root = fake_langfuse.roots[0]
    assert root.name == "agent-agt_ar_assistant-turn"
    assert root.metadata["request_id"] == "req-9"
    assert root.metadata["thread_id"] == "thread-1"
    assert root.ended is True
    assert len(root.children) == 2
    assert root.children[0].name == "agent"  # falls back to node name when there is no tool
    assert root.children[1].name == "erp.query_receivables"
    assert root.children[1].output == "7 rows"
    assert root.children[1].ended is True


def test_pii_scrub_traces_true_redacts_emails_in_args_and_result(fake_langfuse: _FakeLangfuseClient) -> None:
    steps = [_step(args={"note": "contact newcomer@example.com"}, result="đã gửi tới newcomer@example.com")]

    uc1_tracing.export_uc1_trace(
        steps, spec=_traced_spec(pii_scrub_traces=True), principal=Principal(sub="u1", employee_id="E102", roles=[], department=""), thread_id="t1", request_id="r1"
    )

    child = fake_langfuse.roots[0].children[0]
    assert "newcomer@example.com" not in child.input["note"]
    assert "newcomer@example.com" not in child.output


def test_pii_scrub_traces_false_leaves_content_untouched(fake_langfuse: _FakeLangfuseClient) -> None:
    steps = [_step(args={"note": "contact newcomer@example.com"}, result="đã gửi tới newcomer@example.com")]

    uc1_tracing.export_uc1_trace(
        steps, spec=_traced_spec(pii_scrub_traces=False), principal=Principal(sub="u1", employee_id="E102", roles=[], department=""), thread_id="t1", request_id="r1"
    )

    child = fake_langfuse.roots[0].children[0]
    assert child.input["note"] == "contact newcomer@example.com"
    assert child.output == "đã gửi tới newcomer@example.com"


def test_the_trace_is_filed_under_the_turns_request_id(fake_langfuse: _FakeLangfuseClient) -> None:
    """ADR-0053: `agent_runs.trace_id` is the turn's `request_id`, so the PM board can link a row to its trace."""
    uc1_tracing.export_uc1_trace(
        [_step()], spec=_traced_spec(), principal=Principal(sub="u1", employee_id="E102", roles=[], department=""), thread_id="t1", request_id="0123456789abcdef0123456789abcdef"
    )
    assert fake_langfuse.trace_ids == ["0123456789abcdef0123456789abcdef"]


def test_a_broken_langfuse_client_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Explodes:
        def start_observation(self, **kwargs: Any) -> Any:
            raise RuntimeError("Langfuse is down")

    monkeypatch.setattr(uc1_tracing, "_langfuse_client", lambda: _Explodes())
    uc1_tracing.export_uc1_trace(
        [_step()], spec=_traced_spec(), principal=Principal(sub="u1", employee_id="E102", roles=[], department=""), thread_id="t1", request_id="r1"
    )  # must not raise
