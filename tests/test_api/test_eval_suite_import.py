"""Importing/exporting a creator's own test set — ADR-0054 §2, EVAL-2.

Read through HTTP, same discipline as `test_agent_eval.py`. `golden/uc1_ar_v3.jsonl`
is exercised directly (not through the scripted-model machinery, since importing
never touches a model) as the acceptance criterion that names it by path.
"""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from src.models.agent_spec import AgentSpec
from tests.conftest import agent_spec, auth_header

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("default_platform_config")]

_GOLDEN_UC1 = Path(__file__).resolve().parents[2] / "golden" / "uc1_ar_v3.jsonl"


def eval_spec() -> AgentSpec:
    """A one-step agent — the importer never touches its model, so its shape barely matters here."""
    return agent_spec(
        steps=[
            {
                "id": "reply",
                "title": "Trả lời",
                "prompt": "p",
                "model": "gpt-4o",
                "tools": [],
                "max_iterations": 3,
                "next": None,
            }
        ],
        success_criteria=["Trả lời đúng nội dung được hỏi"],
    )


def _base(agent: Any) -> str:
    return f"/api/v1/agents/{agent.id}/eval-suite"


async def _import(client: Any, agent: Any, token: str, *, filename: str, content: bytes, dry_run: bool = False):
    return await client.post(
        f"{_base(agent)}/import",
        headers=auth_header(token),
        params={"dry_run": str(dry_run).lower()},
        files={"file": (filename, content, "application/octet-stream")},
    )


def _csv(rows: list[dict[str, str]], header: list[str] | None = None) -> bytes:
    header = header or ["input", "expected", "criterion", "category", "expected_check", "tags"]
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(_csv_cell(row.get(col, "")) for col in header))
    return ("\n".join(lines)).encode("utf-8")


def _csv_cell(value: str) -> str:
    if "," in value or '"' in value:
        return '"' + value.replace('"', '""') + '"'
    return value


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode("utf-8")


def _xlsx(rows: list[dict[str, Any]], header: list[str] | None = None) -> bytes:
    header = header or ["input", "expected", "criterion", "category", "expected_check", "tags"]
    wb = Workbook()
    sheet = wb.active
    sheet.append(header)
    for row in rows:
        sheet.append([row.get(col, "") for col in header])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


_TWENTY_ROWS = [
    {"input": f"Câu hỏi số {i}", "expected": f"Trả lời đúng số {i}", "criterion": "", "category": "", "expected_check": "", "tags": ""}
    for i in range(1, 21)
]
# Two rows missing `expected` — the AC's "18 added, 2 error".
_TWENTY_ROWS[4]["expected"] = ""
_TWENTY_ROWS[9]["expected"] = ""


async def test_importing_a_csv_reports_added_and_error_rows_and_dry_run_writes_nothing(
    client, creator_token, seeded_creator, drafted_agent_of
) -> None:
    agent, _version = drafted_agent_of(seeded_creator, eval_spec())
    content = _csv(_TWENTY_ROWS)

    preview = await _import(client, agent, creator_token, filename="set.csv", content=content, dry_run=True)
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["dry_run"] is True
    assert payload["added_count"] == 18
    assert payload["error_count"] == 2
    error_rows = {r["row"] for r in payload["rows"] if r["status"] == "error"}
    assert error_rows == {5, 10}

    real = await _import(client, agent, creator_token, filename="set.csv", content=content, dry_run=False)
    assert real.status_code == 200, real.text
    real_payload = real.json()
    assert real_payload["dry_run"] is False
    assert real_payload["added_count"] == 18
    assert real_payload["error_count"] == 2


async def test_dry_run_writes_nothing_to_the_suite(client, creator_token, seeded_creator, drafted_agent_of) -> None:
    agent, version = drafted_agent_of(seeded_creator, eval_spec())
    content = _csv(_TWENTY_ROWS)

    await _import(client, agent, creator_token, filename="set.csv", content=content, dry_run=True)

    listing = await client.get(f"/api/v1/agents/{agent.id}/versions/{version.id}/eval-cases", headers=auth_header(creator_token))
    assert listing.status_code == 200, listing.text
    assert listing.json() == []


async def test_importing_the_same_file_twice_is_all_skipped_the_second_time(
    client, creator_token, seeded_creator, drafted_agent_of
) -> None:
    agent, _version = drafted_agent_of(seeded_creator, eval_spec())
    content = _csv(_TWENTY_ROWS)

    first = await _import(client, agent, creator_token, filename="set.csv", content=content)
    assert first.json()["added_count"] == 18

    second = await _import(client, agent, creator_token, filename="set.csv", content=content)
    payload = second.json()
    assert payload["added_count"] == 0
    assert payload["skipped_count"] == 18
    assert payload["error_count"] == 2


async def test_export_then_import_round_trips_into_another_agent(
    client, creator_token, seeded_creator, drafted_agent_of
) -> None:
    source, _v1 = drafted_agent_of(seeded_creator, eval_spec(), name="Nguồn")
    target, _v2 = drafted_agent_of(seeded_creator, eval_spec(), name="Đích")
    content = _csv(
        [
            {"input": "Đầu vào A", "expected": "Kỳ vọng A", "criterion": "", "category": "", "expected_check": "", "tags": "a;b"},
            {"input": "Đầu vào B", "expected": "Kỳ vọng B", "criterion": "Tiêu chí riêng", "category": "", "expected_check": "", "tags": ""},
        ]
    )
    imported = await _import(client, source, creator_token, filename="s.csv", content=content)
    assert imported.json()["added_count"] == 2

    exported = await client.get(f"{_base(source)}/export", params={"format": "jsonl"}, headers=auth_header(creator_token))
    assert exported.status_code == 200, exported.text

    round_trip = await _import(client, target, creator_token, filename="round.jsonl", content=exported.content)
    assert round_trip.status_code == 200, round_trip.text
    assert round_trip.json()["added_count"] == 2

    source_cases = await client.get(f"/api/v1/agents/{source.id}/versions/{_v1.id}/eval-cases", headers=auth_header(creator_token))
    target_cases = await client.get(f"/api/v1/agents/{target.id}/versions/{_v2.id}/eval-cases", headers=auth_header(creator_token))
    source_pairs = sorted((c["input_message"], c["criterion"]) for c in source_cases.json())
    target_pairs = sorted((c["input_message"], c["criterion"]) for c in target_cases.json())
    assert source_pairs == target_pairs


async def test_the_three_formats_with_the_same_content_produce_the_same_result(
    client, creator_token, seeded_creator, drafted_agent_of
) -> None:
    rows = [
        {"input": "Định dạng nào cũng đọc như nhau", "expected": "Kết quả giống hệt", "criterion": "", "category": "", "expected_check": "", "tags": "x"}
    ]
    results = {}
    for fmt, (filename, content) in {
        "csv": ("a.csv", _csv(rows)),
        "jsonl": ("a.jsonl", _jsonl(rows)),
        "xlsx": ("a.xlsx", _xlsx(rows)),
    }.items():
        agent, _version = drafted_agent_of(seeded_creator, eval_spec(), name=f"Agent {fmt}")
        response = await _import(client, agent, creator_token, filename=filename, content=content)
        assert response.status_code == 200, response.text
        results[fmt] = response.json()["added_count"]

    assert results == {"csv": 1, "jsonl": 1, "xlsx": 1}


async def test_more_than_500_rows_is_rejected_with_422(client, creator_token, seeded_creator, drafted_agent_of) -> None:
    agent, _version = drafted_agent_of(seeded_creator, eval_spec())
    rows = [{"input": f"q{i}", "expected": f"e{i}", "criterion": "", "category": "", "expected_check": "", "tags": ""} for i in range(501)]
    response = await _import(client, agent, creator_token, filename="huge.csv", content=_csv(rows))
    assert response.status_code == 422
    assert "500" in response.text


async def test_a_bad_row_does_not_break_the_others_in_the_same_file(
    client, creator_token, seeded_creator, drafted_agent_of
) -> None:
    rows = [
        {"input": "Câu 1 hợp lệ", "expected": "Đạt 1", "criterion": "", "category": "", "expected_check": "", "tags": ""},
        {"input": "", "expected": "Không có input", "criterion": "", "category": "", "expected_check": "", "tags": ""},
        {"input": "Câu 3 hợp lệ", "expected": "Đạt 3", "criterion": "", "category": "", "expected_check": "", "tags": ""},
    ]
    agent, _version = drafted_agent_of(seeded_creator, eval_spec())
    response = await _import(client, agent, creator_token, filename="mix.csv", content=_csv(rows))
    payload = response.json()
    assert payload["added_count"] == 2
    assert payload["error_count"] == 1
    assert [r["status"] for r in payload["rows"]] == ["added", "error", "added"]


async def test_the_uc1_golden_set_imports_through_the_general_importer_with_extra_preserved(
    client, creator_token, seeded_creator, drafted_agent_of, db_session
) -> None:
    """`golden/uc1_ar_v3.jsonl` through *this general* importer — every row accounted for, nothing lost to `extra`.

    Not 23 `added`: three rows (#1, #21, #22) share the exact literal `input`
    text ("Khách nào quá hạn trên 30 ngày?") but differ only in `principal` —
    who is asking, which for UC-1 changes what "correct" means (row-level
    security scope). This importer's declared dedup key is `(input,
    criterion)` (ADR-0054 §2), and none of these rows carries a `criterion`,
    so two of the three collide under that key and land as `skipped` rather
    than `error` — no data lost, just not distinct under this key. Giving
    UC-1's golden set its own identity (`extra.id`/`principal`-aware dedup) is
    EVAL-7 (#157)'s dedicated `scripts/import_golden_set.py`, not this
    general-purpose endpoint.
    """
    agent, version = drafted_agent_of(seeded_creator, eval_spec())
    content = _GOLDEN_UC1.read_bytes()

    response = await _import(client, agent, creator_token, filename="uc1_ar_v3.jsonl", content=content)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["added_count"] == 21
    assert payload["skipped_count"] == 2
    assert payload["error_count"] == 0

    listing = await client.get(f"/api/v1/agents/{agent.id}/versions/{version.id}/eval-cases", headers=auth_header(creator_token))
    assert len(listing.json()) == 21

    from src.evals.cases import list_cases

    stored = list_cases(db_session, agent.id)
    assert len(stored) == 21
    assert all(case.extra and "principal" in case.extra for case in stored)


async def test_template_csv_downloads_with_header_and_examples(client, creator_token, seeded_creator, drafted_agent_of) -> None:
    agent, _version = drafted_agent_of(seeded_creator, eval_spec())
    response = await client.get(f"{_base(agent)}/template.csv", headers=auth_header(creator_token))
    assert response.status_code == 200, response.text
    lines = response.text.strip().splitlines()
    assert lines[0].split(",")[:3] == ["input", "expected", "criterion"]
    assert len(lines) == 4  # header + 3 example rows
