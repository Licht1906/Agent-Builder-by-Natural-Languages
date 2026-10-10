"""The branch judge reasons first and decides second.

A real transcript line, agent 23's "đã đủ thông tin?" gate, on a turn whose
collector had just written *"email người được mời: còn thiếu"* and asked for
the address: `→ true: Chưa có email người được mời nên chưa đủ điều kiện`. The
verdict and the reason disagreed, and the run went down the acting branch,
composed a mail to nobody and stopped at approval. The prompt had asked for
`holds` before `reason`, so the model committed and then reasoned. Reversed,
the verdict is written after the reasoning it must agree with.
"""

from src.workflow.runtime import _CONDITION_PROMPT


def test_the_reason_key_comes_before_the_verdict_key() -> None:
    rendered = _CONDITION_PROMPT.format(condition="đã đủ thông tin", context="(không)")

    assert rendered.index('"reason"') < rendered.index('"holds"')
    assert "đúng thứ tự khoá" in rendered


def test_the_judge_is_told_that_a_detail_just_asked_for_is_not_there_yet() -> None:
    rendered = _CONDITION_PROMPT.format(condition="đã đủ thông tin", context="(không)")

    assert '"còn thiếu"' in rendered
    assert "vừa hỏi người dùng về nó thì thông tin đó chưa có" in rendered
