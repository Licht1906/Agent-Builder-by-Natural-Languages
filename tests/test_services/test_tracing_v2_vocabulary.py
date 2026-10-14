"""`EventTrace` folds a V2 workflow's event stream into the same tree as a V1 run's.

`src.workflow.runtime`'s own docstring says "what is *not* shared is the event
vocabulary": a V1 run reports `step_id`s and closes a unit with `step`, a V2
run reports `node_id`s, opens with `node_started` and closes with `node`. Until
ADR-0053's trace work the tracer only knew the first dialect, so a V2 run
exported to Langfuse as a root span with every tool call hanging off it and no
node in between — a flat list where the canvas shows a graph.

What is pinned here is the inversion itself, on the production class
(`RecordingTrace` in `conftest` subclasses it for the HTTP tests), and the
Langfuse export's shape through a fake client: one span per node, the tool
inside its node, the branch taken and the node type on the span, a paused leg
recorded as paused rather than as success.
"""

from dataclasses import dataclass, field
from typing import Any

from src.agents.budget import ModelUsage
from src.agents.costs import CostBasis, CostFigure
from src.agents.threads import RunIdentity, ThreadNamespace, ThreadRef
from src.services import tracing
from src.services.tracing import EventTrace, LangfuseTrace, RecordedRun


def _identity() -> RunIdentity:
    return RunIdentity(
        thread=ThreadRef(agent_id=7, conversation_id="c1", namespace=ThreadNamespace.PRODUCTION, caller="u3"),
        version_id=13,
        trace_id="0123456789abcdef0123456789abcdef",
    )


def _cost() -> CostFigure:
    return CostFigure(basis=CostBasis.MEASURED, usage=ModelUsage(100, 20), usd=0.001, seconds=1.5)


class _Keeps(EventTrace):
    """The production inversion, keeping its result."""

    def __init__(self, identity: RunIdentity) -> None:
        self.run: RecordedRun | None = None
        super().__init__(identity)

    def run_ended(self, run: RecordedRun) -> None:
        self.run = run


def _v2_document_qa(trace: EventTrace) -> None:
    """The `document_qa` template's stream: search (one tool) → answer → end."""
    trace.record("node_started", {"node_id": "search", "node_type": "knowledge_search"})
    trace.record(
        "tool", {"node_id": "search", "tool": "knowledge_search", "args": {"query": "hoàn tiền"}, "simulated": False}
    )
    trace.record(
        "tool_result",
        {
            "node_id": "search",
            "tool": "knowledge_search",
            "ok": True,
            "result": "7 ngày",
            "simulated": False,
            "citations": [],
        },
    )
    trace.record(
        "node",
        {
            "node_id": "search",
            "node_type": "knowledge_search",
            "content": "Tìm thấy 1 đoạn",
            "port_taken": "found",
            "iteration": None,
            "forced_exit": None,
        },
    )
    trace.record("node_started", {"node_id": "answer", "node_type": "agent_llm"})
    trace.record("token", {"node_id": "answer", "text": "Hoàn "})
    trace.record("usage", {"basis": "measured"})
    trace.record(
        "node",
        {
            "node_id": "answer",
            "node_type": "agent_llm",
            "content": "Hoàn tiền trong 7 ngày.",
            "port_taken": None,
            "iteration": None,
            "forced_exit": None,
        },
    )
    trace.record("node_started", {"node_id": "finish", "node_type": "end"})
    trace.record(
        "node",
        {
            "node_id": "finish",
            "node_type": "end",
            "content": "",
            "port_taken": None,
            "iteration": None,
            "forced_exit": None,
        },
    )


def test_a_v2_stream_resolves_to_one_unit_per_node_with_the_tool_inside_its_node() -> None:
    trace = _Keeps(_identity())
    _v2_document_qa(trace)
    trace.finish(cost=_cost(), error=None)

    run = trace.run
    assert run is not None
    assert [s.step_id for s in run.steps] == ["search", "answer", "finish"]
    assert [s.node_type for s in run.steps] == ["knowledge_search", "agent_llm", "end"]
    search, answer, finish = run.steps
    assert [t.tool for t in search.tools] == ["knowledge_search"]
    assert search.tools[0].ok is True and search.tools[0].result == "7 ngày"
    assert search.port_taken == "found"
    assert search.output == "Tìm thấy 1 đoạn"
    assert answer.tools == [] and answer.output == "Hoàn tiền trong 7 ngày."
    assert all(s.ended_at is not None for s in (search, answer, finish))
    assert tracing._outcome_of(run) == "success"


def test_a_v1_stream_still_resolves_exactly_as_before() -> None:
    """The old dialect is untouched: steps keyed by `step_id`, titled by `step`."""
    trace = _Keeps(_identity())
    trace.record("token", {"step_id": "s1", "text": "x"})
    trace.record("tool", {"step_id": "s1", "tool": "web_search", "args": {}, "simulated": True})
    trace.record("tool_result", {"step_id": "s1", "tool": "web_search", "ok": False, "result": "timeout"})
    trace.record("step", {"step_id": "s1", "title": "Bước 1", "content": "xong 1"})
    trace.record("step", {"step_id": "s2", "title": "Bước 2", "content": "xong 2"})
    trace.finish(cost=_cost(), error=None)

    run = trace.run
    assert run is not None
    assert [(s.step_id, s.title, s.output, s.node_type) for s in run.steps] == [
        ("s1", "Bước 1", "xong 1", None),
        ("s2", "Bước 2", "xong 2", None),
    ]
    assert run.steps[0].tools[0].simulated is True and run.steps[0].tools[0].ok is False


def test_each_pass_of_a_while_body_is_its_own_unit() -> None:
    """`node_started` opens afresh even for an id already seen — ADR-0026 §4."""
    trace = _Keeps(_identity())
    for i in (1, 2):
        trace.record("node_started", {"node_id": "loop", "node_type": "while"})
        trace.record(
            "node",
            {
                "node_id": "loop",
                "node_type": "while",
                "content": f"vòng {i}",
                "port_taken": "loop",
                "iteration": i,
                "forced_exit": None,
            },
        )
        trace.record("node_started", {"node_id": "body", "node_type": "agent_llm"})
        trace.record(
            "node",
            {
                "node_id": "body",
                "node_type": "agent_llm",
                "content": f"lần {i}",
                "port_taken": None,
                "iteration": None,
                "forced_exit": None,
            },
        )
    trace.finish(cost=_cost(), error=None)

    assert [s.step_id for s in trace.run.steps] == ["loop", "body", "loop", "body"]  # type: ignore[union-attr]


def test_a_paused_leg_is_recorded_as_awaiting_approval_not_success() -> None:
    trace = _Keeps(_identity())
    trace.record("node_started", {"node_id": "duyet", "node_type": "human_approval"})
    trace.record("awaiting_approval", {"node_id": "duyet", "approval_id": 55, "message": "Gửi 3 thư?"})
    trace.finish(cost=_cost(), error=None)

    run = trace.run
    assert run is not None
    assert run.paused_at == "duyet"
    assert tracing._outcome_of(run) == "awaiting_approval"
    # The node it paused at is the open unit when `finish` arrives: exported,
    # but with no end of its own — the run stopped inside it, and that
    # unfinished span is the most informative thing in the trace.
    assert run.steps[-1].step_id == "duyet" and run.steps[-1].ended_at is None


def test_a_budget_stop_still_outranks_the_other_endings() -> None:
    trace = _Keeps(_identity())
    trace.record("node_started", {"node_id": "loop", "node_type": "while"})
    trace.record(
        "budget_exceeded",
        {"node_id": "loop", "limit": "max_total_tokens", "limit_value": 10, "used": 12, "message": "quá trần"},
    )
    trace.finish(cost=_cost(), error="RuntimeError: quá trần")

    assert tracing._outcome_of(trace.run) == "budget_stopped"  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The Langfuse export of the same stream
# --------------------------------------------------------------------------- #


@dataclass
class _Span:
    name: str
    as_type: str
    input: Any = None
    output: Any = None
    level: str | None = None
    metadata: dict = field(default_factory=dict)
    trace_context: Any = None
    ended: bool = False
    children: list["_Span"] = field(default_factory=list)
    trace_updates: dict = field(default_factory=dict)

    def start_observation(
        self,
        *,
        name: str,
        as_type: str = "span",
        input: Any = None,
        metadata: dict | None = None,
        trace_context: Any = None,
    ) -> "_Span":  # noqa: A002
        child = _Span(
            name=name, as_type=as_type, input=input, metadata=dict(metadata or {}), trace_context=trace_context
        )
        self.children.append(child)
        return child

    def update(
        self, *, name: str | None = None, output: Any = None, metadata: dict | None = None, level: str | None = None
    ) -> None:
        if name is not None:
            self.name = name
        self.output = output
        self.level = level
        self.metadata.update(metadata or {})

    def update_trace(self, **kwargs: Any) -> None:
        self.trace_updates.update(kwargs)

    def end(self) -> None:
        self.ended = True


def test_the_langfuse_export_nests_tools_under_nodes_and_types_the_nodes() -> None:
    client = _Span(name="client", as_type="client")
    trace = LangfuseTrace(_identity(), client)
    _v2_document_qa(trace)
    trace.finish(cost=_cost(), error=None)

    root = client.children[0]
    assert root.trace_context["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert root.as_type == "agent" and root.ended
    assert root.trace_updates["session_id"] == "agent-7-c1" and root.trace_updates["user_id"] == "u3"
    assert [(c.name, c.as_type) for c in root.children] == [
        ("search", "retriever"),
        ("answer", "generation"),
        ("finish", "chain"),
    ]
    search, answer, _finish = root.children
    assert [(t.name, t.as_type, t.output) for t in search.children] == [("knowledge_search", "tool", "7 ngày")]
    assert search.metadata == {"node_type": "knowledge_search", "port_taken": "found"}
    assert answer.output == "Hoàn tiền trong 7 ngày." and answer.metadata == {"node_type": "agent_llm"}
    assert root.metadata["outcome"] == "success" and root.metadata["total_tokens"] == 120
    assert all(c.ended for c in root.children)


def test_the_langfuse_export_of_a_paused_leg_says_so_on_the_root() -> None:
    client = _Span(name="client", as_type="client")
    trace = LangfuseTrace(_identity(), client)
    trace.record("node_started", {"node_id": "duyet", "node_type": "human_approval"})
    trace.record("awaiting_approval", {"node_id": "duyet", "approval_id": 1, "message": "?"})
    trace.finish(cost=_cost(), error=None)

    root = client.children[0]
    assert root.metadata["outcome"] == "awaiting_approval"
    assert root.output == "awaiting approval at duyet"
    assert root.level is None
    assert [(c.name, c.as_type) for c in root.children] == [("duyet", "guardrail")]


def test_trace_url_template_needs_keys_and_a_project(monkeypatch: Any) -> None:
    settings = tracing.get_settings()
    monkeypatch.setattr(settings, "langfuse_public_key", "pk")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk")
    monkeypatch.setattr(settings, "langfuse_host", "http://localhost:3000/")
    monkeypatch.setattr(settings, "langfuse_project_id", "")
    assert tracing.trace_url_template() is None
    monkeypatch.setattr(settings, "langfuse_project_id", "agentlab-local")
    assert tracing.trace_url_template() == "http://localhost:3000/project/agentlab-local/traces/{trace_id}"
    monkeypatch.setattr(settings, "langfuse_secret_key", "")
    assert tracing.trace_url_template() is None
