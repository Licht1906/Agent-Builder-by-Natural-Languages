"""`repair_tool_call_arguments` — and the two seams that must apply it.

Every assertion here is made against `langchain_openai`'s own
`_convert_message_to_dict`, never against the `AIMessage`'s attributes alone.
That is the whole point of the bug being fixed: the raw string survives in
`additional_kwargs` even after `tool_calls`/`invalid_tool_calls` look clean, and
the converter falls back to it. Only the dict that actually goes on the wire can
say whether the malformed arguments are really gone.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.tool import tool_call_chunk
from langchain_openai.chat_models.base import _convert_message_to_dict

from src.agents.runtime import _stream_model_answer
from src.services.tool_call_repair import repair_tool_call_arguments


def _reply_with_raw_arguments(arguments: str) -> AIMessage:
    """A reply carrying `arguments` verbatim, built the way the provider path builds one.

    Raw `additional_kwargs["tool_calls"]` and nothing else — `AIMessage`'s own
    validator sorts it into `tool_calls`/`invalid_tool_calls` using exactly the
    parser `langchain_openai` uses, so the fixture cannot drift from the real
    thing by deciding for itself which list a given string belongs in.
    """
    return AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "erp.query", "arguments": arguments}}]
        },
    )


def _wire_tool_calls(message: AIMessage) -> Any:
    return _convert_message_to_dict(message).get("tool_calls")


def test_the_unrepaired_message_is_what_the_provider_rejects() -> None:
    """The bug itself, pinned: without repair the malformed string goes back out as-is.

    Guards the fix against being quietly undone — if `langchain_openai` ever
    stopped echoing invalid tool calls, this test fails and the repair becomes
    dead code worth deleting rather than dead code worth keeping.
    """
    raw = '{"query": "v_ar_open"} {"query": "x"}'

    assert _wire_tool_calls(_reply_with_raw_arguments(raw))[0]["function"]["arguments"] == raw


def test_trailing_garbage_is_trimmed_to_the_object_the_model_meant() -> None:
    repaired = repair_tool_call_arguments(_reply_with_raw_arguments('{"query": "v_ar_open"} {"query": "x"}'))

    assert repaired.tool_calls[0]["args"] == {"query": "v_ar_open"}
    assert _wire_tool_calls(repaired) == [
        {"type": "function", "id": "c1", "function": {"name": "erp.query", "arguments": '{"query": "v_ar_open"}'}}
    ]


@pytest.mark.parametrize("arguments", ["", "   ", "khách hàng quá hạn 30 ngày"])
def test_arguments_that_are_not_a_json_object_leave_nothing_on_the_wire(arguments: str) -> None:
    repaired = repair_tool_call_arguments(_reply_with_raw_arguments(arguments))

    assert not repaired.tool_calls
    assert not repaired.invalid_tool_calls
    # The `additional_kwargs` fallback is the half that a mutating fix would miss.
    assert _wire_tool_calls(repaired) is None


def test_a_well_formed_reply_is_handed_back_untouched() -> None:
    """Identity, not an equal copy: the overwhelmingly common path must not pay for
    a message rebuild, and must not lose anything a rebuild would have to re-copy.
    """
    reply = _reply_with_raw_arguments('{"query": "v_ar_open"}')

    assert repair_tool_call_arguments(reply) is reply


def test_a_valid_call_beside_a_malformed_one_survives_the_repair() -> None:
    reply = AIMessage(
        content="",
        additional_kwargs={
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "erp.query", "arguments": '{"days": 30}'}},
                {"id": "c2", "type": "function", "function": {"name": "mail.send", "arguments": '{"to": "a"} junk'}},
            ]
        },
    )

    repaired = repair_tool_call_arguments(reply)

    assert [(call["id"], call["args"]) for call in repaired.tool_calls] == [
        ("c1", {"days": 30}),
        ("c2", {"to": "a"}),
    ]


def test_the_streaming_runtime_repairs_the_merged_message() -> None:
    """`_stream_model_answer` is where V1's step loop and V2's `AgentLlm` node share a
    model call (`charged_model_call`), so it is the one place either runtime can
    take a malformed call into its history.

    Split across two chunks on purpose: mid-stream `arguments` are *unfinished*,
    not malformed, and repairing before the merge would mangle every streamed
    tool call in the platform.
    """
    chunks = [
        AIMessageChunk(
            content="", tool_call_chunks=[tool_call_chunk(name="erp.query", args='{"days":', id="c1", index=0)]
        ),
        AIMessageChunk(content="", tool_call_chunks=[tool_call_chunk(args='30} {"days": 9}', index=0)]),
    ]

    merged = asyncio.run(_stream_model_answer(_StreamingStub(chunks), [], on_text=lambda _text: None))

    assert merged.tool_calls[0]["args"] == {"days": 30}
    assert _wire_tool_calls(merged)[0]["function"]["arguments"] == '{"days": 30}'


class _StreamingStub:
    """The narrowest thing `_stream_model_answer` actually uses: something with `astream`."""

    def __init__(self, chunks: list[AIMessageChunk]) -> None:
        self._chunks = chunks

    async def astream(self, _messages: list[BaseMessage], **_kwargs: object) -> AsyncIterator[BaseMessage]:
        for chunk in self._chunks:
            yield chunk
