"""Salvage what is salvageable from a model's malformed tool-call arguments, and
never let the unsalvageable back onto the wire.

Small models mis-emit tool arguments. The common failure is a correct JSON
object followed by a second one, or by a stray fragment, in the one `arguments`
string. LangChain does not raise on that — it files the call under
`AIMessage.invalid_tool_calls` with the raw string kept verbatim, and
`langchain_openai._lc_invalid_tool_call_to_openai_tool_call` puts that same raw
string back into the request the next time the conversation is sent.

That is what makes it worth a module rather than a defensive line at one call
site: the damage is not to the turn that produced it, it is to *every later
turn*. A checkpointed thread replays its whole history, so one malformed call
poisons the conversation from that point on — and providers do reject it.
OpenRouter's own `qwen/qwen-2.5-7b-instruct` provider `json.loads` the arguments
of every assistant message it is sent and answers `400 invalid_request_error:
Extra data: line 1 column 23 (char 22)`, raised from inside whichever graph node
made the call. To a creator that reads as "the deployed agent is broken", not
"one turn parsed badly".

Lives in `src.services` because the two runtimes that need it cannot reach each
other: `src.compiler.graph` compiles UC-1 specs, `src.agents.runtime` runs V1
steps (and, through `charged_model_call`, V2's nodes), and neither imports the
other. A leaf module both already depend on the package of is the only place one
copy of this can live — and one copy is the point, since two would eventually
disagree about what "recoverable" means.
"""

import json
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.messages.tool import ToolCall, tool_call


def repair_tool_call_arguments(reply: AIMessage) -> AIMessage:
    """`reply` with its `invalid_tool_calls` either repaired into real tool calls or removed.

    `raw_decode` rather than `loads` is the whole repair: it consumes the leading
    JSON value and *stops*, which is exactly the model's intent in the
    trailing-garbage case — the call it meant to make, minus the noise. A
    repaired call is a call this graph then executes, so a turn that would have
    died completes normally.

    Anything that does not begin with a JSON object is dropped instead of
    guessed at. A tool call whose arguments cannot be read is one no runtime
    should execute on invented input, and dropping leaves the model a turn to try
    again — where echoing it back leaves the thread with a 400 it never recovers
    from.

    Returns a *new* message rather than mutating, because `additional_kwargs`
    still holds the raw provider payload: `_convert_message_to_dict` falls back
    to `additional_kwargs["tool_calls"]` the moment both typed lists are empty,
    which would send the very string this function just removed.
    """
    if not reply.invalid_tool_calls:
        return reply

    repaired: list[ToolCall] = list(reply.tool_calls)
    for invalid in reply.invalid_tool_calls:
        args = _leading_json_object(invalid["args"])
        if args is None or not invalid["name"]:
            continue
        repaired.append(tool_call(name=invalid["name"], args=args, id=invalid["id"]))

    return AIMessage(
        content=reply.content,
        tool_calls=repaired,
        additional_kwargs={k: v for k, v in reply.additional_kwargs.items() if k != "tool_calls"},
        response_metadata=reply.response_metadata,
        usage_metadata=reply.usage_metadata,
        id=reply.id,
    )


def _leading_json_object(raw: str | None) -> dict[str, Any] | None:
    """The JSON object `raw` starts with, ignoring whatever follows it — or `None`
    if it does not start with one at all (empty string, a bare scalar, prose).
    """
    if not raw:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(raw.strip())
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
