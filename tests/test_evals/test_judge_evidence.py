"""The judge reads what the run's tools returned, not only what the agent said about it.

Ten Google agents built through the meta agent and evaluated for real: cases
failed with *"đã bịa ra tên tài khoản"* on a graph whose first node was the
`MCP` call, and *"không có bằng chứng cho thấy đây là kết quả thật"* on
answers that quoted the live listing verbatim. The judge had been shown the
answer alone. These pin the repair: the transcript's tool lines reach the
judge, paired call-to-result, marked when simulated, and absent (not an empty
heading) when the run called nothing.
"""

from src.evals.judge import ToolEvidence, rendered_evidence
from src.evals.workflow_runner import tool_evidence
from src.models.schemas import SandboxTranscriptEntry


def test_no_calls_means_no_block_at_all() -> None:
    assert rendered_evidence([]) == ""


def test_each_call_is_shown_with_its_arguments_and_result() -> None:
    block = rendered_evidence(
        [
            ToolEvidence(
                tool="gmail.get_profile", args={}, ok=True, result='{"emailAddress": "a@b.vn", "messagesTotal": 3364}'
            )
        ]
    )

    assert "Công cụ agent đã gọi" in block
    assert '- gọi gmail.get_profile với {} → ok: {"emailAddress": "a@b.vn", "messagesTotal": 3364}' in block
    assert "không phải bịa" in block
    assert "(kết quả mô phỏng)" not in block


def test_a_simulated_result_is_labelled_so_the_judge_does_not_fail_the_agent_for_a_mock_id() -> None:
    block = rendered_evidence(
        [
            ToolEvidence(
                tool="google_drive.create_doc",
                args={"title": "x"},
                ok=True,
                result='{"file_id": "doc-mo-phong-001"}',
                simulated=True,
            )
        ]
    )

    assert '→ ok (kết quả mô phỏng): {"file_id": "doc-mo-phong-001"}' in block


def test_a_failed_call_says_so_and_a_long_result_is_cut() -> None:
    block = rendered_evidence(
        [ToolEvidence(tool="google_drive.fetch", args={"file_id": "1"}, ok=False, result="x" * 9000)]
    )

    assert "→ lỗi: " in block
    assert "x" * 6000 + " […ĐÃ CẮT" in block
    assert "x" * 6001 not in block
    # The cut is named, so an answer naming what lies past it is not "bịa".
    assert "KHÔNG kết luận là bịa" in block


def test_transcript_lines_are_paired_call_to_result_in_run_order() -> None:
    transcript = [
        SandboxTranscriptEntry(kind="node", node_id="collect", node_type="agent_llm", content="max_results: 3"),
        SandboxTranscriptEntry(
            kind="tool", node_id="list", tool="gmail.get_recent_emails", args={"max_results": 3}, simulated=False
        ),
        SandboxTranscriptEntry(
            kind="tool_result",
            node_id="list",
            tool="gmail.get_recent_emails",
            ok=True,
            simulated=False,
            content='{"messages": []}',
        ),
        SandboxTranscriptEntry(
            kind="tool", node_id="draft", tool="gmail.create_draft", args={"to": "a@b.vn"}, simulated=True
        ),
        SandboxTranscriptEntry(
            kind="tool_result",
            node_id="draft",
            tool="gmail.create_draft",
            ok=True,
            simulated=True,
            content='{"draft_id": "draft-mo-phong-001"}',
        ),
    ]

    assert tool_evidence(transcript) == [
        ToolEvidence(
            tool="gmail.get_recent_emails", args={"max_results": 3}, ok=True, result='{"messages": []}', simulated=False
        ),
        ToolEvidence(
            tool="gmail.create_draft",
            args={"to": "a@b.vn"},
            ok=True,
            result='{"draft_id": "draft-mo-phong-001"}',
            simulated=True,
        ),
    ]


def test_a_call_the_run_never_finished_is_still_reported() -> None:
    transcript = [
        SandboxTranscriptEntry(kind="tool", node_id="n", tool="gmail.send_email", args={"to": "x"}, simulated=False)
    ]

    [only] = tool_evidence(transcript)
    assert only.ok is None
    assert only.result == "(không có kết quả ghi lại)"


def test_the_required_parameters_travel_with_the_call() -> None:
    """`create_doc` requires `title` and `content`; a judge that knows this does not fail an agent for asking for them."""
    block = rendered_evidence(
        [ToolEvidence(tool="google_drive.create_doc", args={}, ok=True, result="{}", required=("title", "content"))]
    )

    assert "- gọi google_drive.create_doc [tham số bắt buộc của action: title, content] với {}" in block


def test_a_collector_stop_is_explained_to_the_judge_as_a_required_parameter() -> None:
    """No tool ran, so no evidence line — the note is how the judge learns `query` was required."""
    from src.evals.workflow_runner import collector_notes
    from src.models.workflow import WorkflowJSON

    graph = WorkflowJSON.model_validate(
        {
            "start_node_id": "thu_thap",
            "nodes": [
                {"id": "thu_thap", "type": "collect", "config": {"action_node": "tim"}},
                {"id": "tim", "type": "mcp", "config": {"tool_id": "gmail", "action": "search_emails"}},
                {"id": "xong", "type": "end", "config": {}},
            ],
            "edges": [
                {"id": "e1", "source_node_id": "thu_thap", "source_port": "complete", "target_node_id": "tim"},
                {"id": "e2", "source_node_id": "thu_thap", "source_port": "incomplete", "target_node_id": "xong"},
                {"id": "e3", "source_node_id": "tim", "source_port": "done", "target_node_id": "xong"},
            ],
            "metadata": {},
        }
    )
    transcript = [
        SandboxTranscriptEntry(
            kind="node", node_id="thu_thap", node_type="collect", content="Bạn vui lòng…", port_taken="incomplete"
        ),
        SandboxTranscriptEntry(kind="node", node_id="xong", node_type="end", content=""),
    ]

    [note] = collector_notes(transcript, graph)
    assert "gmail.search_emails BẮT BUỘC có query" in note
    assert "không phải hỏi thừa" in note
    assert rendered_evidence([], [note]).count("- Agent dừng ở bước thu thập") == 1
