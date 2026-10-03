"""The slot-filling interview (#16), read through HTTP.

Every model here is a scripted double, which is the ticket's last criterion — and
the two shapes it names are the two this file is built around: a description
detailed enough that nothing is left to ask, and a bare one that leaves every slot
empty. They exercise opposite ends of the same code path, and the interesting
claim is that neither of them involves the model deciding anything.

What the doubles are allowed to influence is which slots get *filled*. What they
cannot influence — and what most of these tests are really about — is when the
interview stops, what it asks next, and whether it asks something twice. Those
come from `src.interview.slots`, which has no model in it at all
(`tests/test_interview/test_slots.py`).
"""

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from tests.conftest import auth_header

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

#: A description that answers nothing — the *"mô tả sơ sài"* half of the ticket.
SPARSE_DESCRIPTION = "Tôi muốn một agent trả lời khách hàng."

ALL_SLOTS = ["audience", "inputs", "capabilities", "success_criteria", "prohibitions"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def reads(**values: Any) -> AIMessage:
    """One extraction reply: the JSON object the meta-agent returns for a passage."""
    return AIMessage(content=json.dumps(values, ensure_ascii=False))


async def start(client: Any, token: str, description: str) -> Any:
    return await client.post("/api/v1/interviews", json={"description": description}, headers=auth_header(token))


async def answer(client: Any, token: str, interview_id: int, text: str) -> Any:
    return await client.post(
        f"/api/v1/interviews/{interview_id}/answers", json={"answer": text}, headers=auth_header(token)
    )


async def read_interview(client: Any, token: str, interview_id: int) -> Any:
    return await client.get(f"/api/v1/interviews/{interview_id}", headers=auth_header(token))


async def a_sparse_interview(client: Any, token: str, factory: Any) -> dict[str, Any]:
    """An interview whose description filled nothing — five questions still to go."""
    factory([[reads()]])
    response = await start(client, token, SPARSE_DESCRIPTION)
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Answering, and stopping
# --------------------------------------------------------------------------- #


async def test_the_interview_stops_the_moment_the_last_slot_is_filled(
    client, creator_token, scripted_chat_model_factory
) -> None:
    """Five empty slots, five answers, then it is over — and it is over on arithmetic.

    Nothing in the loop below asks a model whether to continue. The stop is
    `remaining == 0`, which is why the same script always ends in the same place.
    """
    interview = await a_sparse_interview(client, creator_token, scripted_chat_model_factory)
    scripted_replies = {
        "audience": reads(audience="nhân viên CSKH"),
        "inputs": reads(inputs="email khiếu nại"),
        "capabilities": reads(capabilities=["tra cứu đơn hàng"]),
        "success_criteria": reads(success_criteria=["nêu đúng mã đơn hàng"]),
        "prohibitions": reads(prohibitions=["không tự ý hoàn tiền"]),
    }

    payload = interview
    asked: list[str] = []
    while not payload["complete"]:
        slot = payload["next_question"]["slot"]
        asked.append(slot)
        scripted_chat_model_factory([[scripted_replies[slot]]])
        response = await answer(client, creator_token, interview["id"], "câu trả lời của người tạo")
        assert response.status_code == 200, response.text
        payload = response.json()

    assert asked == ALL_SLOTS
    assert payload["remaining"] == 0
    assert payload["next_question"] is None


# --------------------------------------------------------------------------- #
# Whose interview it is, and whose model runs it
# --------------------------------------------------------------------------- #


async def test_another_creators_interview_is_not_found(
    client, creator_token, second_creator_token, scripted_chat_model_factory
) -> None:
    """404 rather than 403: a session id is guessable, and "yours or not" is a leak.

    What it would leak is what other people in the platform are building, which is
    the same thing `OwnedAgent` refuses to disclose.
    """
    interview = await a_sparse_interview(client, creator_token, scripted_chat_model_factory)

    response = await read_interview(client, second_creator_token, interview["id"])

    assert response.status_code == 404
