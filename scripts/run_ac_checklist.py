#!/usr/bin/env python3
"""UC1-37 (#149) — turns spec.md §18's acceptance table into something that
runs AC-1 through AC-12 in order and writes the evidence tickets.md's own
ticket asks for: "chạy tuần tự AC-1 → AC-12, ghi lại bằng chứng (log/lệnh
SQL/thời gian bấm) cho từng cái."

Every AC already has real, automated coverage somewhere in this repo —
`docs/usecase_spec/uc1-ar-assistant/spec.md`'s own §20 explains why
(`scripts/verify_uc1_data_surface.py`'s docstring: "cùng một test vừa phục vụ
CI vừa phục vụ thuyết trình"). This script does not duplicate that coverage —
it runs the pytest node ids and standalone scripts that already prove each
AC, in the order §18 lists them, and turns "ran, exit 0" into the dated,
readable evidence record a nghiệm thu hội đồng actually wants to see, rather
than a green checkmark in a CI log nobody opens twice.

Three of the twelve (AC-8, AC-9, AC-10) genuinely cannot be answered by
anything this script can run in-process: AC-8 needs a live deployed
container to `docker exec` into, AC-9 needs a real LLM (every automated test
in this repo scripts a fake one, on purpose — see `tests/conftest.py`'s own
`scripted_chat_model_factory`), and AC-10 needs an actual blank machine and a
stopwatch. Their evidence has to come from a human, supplied with
`--manual-evidence AC-8="<output/timing>"` — omitted, they are reported
PENDING, not silently skipped, and the checklist can never read "12/12
xanh" without them.

Usage:
    python scripts/run_ac_checklist.py
    python scripts/run_ac_checklist.py --manual-evidence 'AC-8=docker exec agt_ar_assistant nslookup evil.example → NXDOMAIN'
    python scripts/run_ac_checklist.py --out docs/usecase_spec/uc1-ar-assistant/ac_checklist_evidence/2026-08-15.md

Exit code 0 only when all 12 are green (every automated check passed and
every manual one has evidence attached); 1 otherwise.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_EVIDENCE_DIR = _REPO_ROOT / "docs" / "usecase_spec" / "uc1-ar-assistant" / "ac_checklist_evidence"

#: How much of a failing command's own stdout/stderr to keep in the report —
#: enough to see what broke without the report growing unboundedly on a
#: verbose pytest failure.
_OUTPUT_TAIL_LINES = 60


@dataclass(frozen=True, slots=True)
class ACCheck:
    id: str
    #: spec.md §18's own "Tiêu chí" column, verbatim.
    title: str
    #: spec.md §18's own "Cách kiểm chứng" column, verbatim.
    verification: str
    #: pytest node ids already proving this AC — run in order, all must pass.
    pytest_targets: tuple[str, ...] = ()
    #: A standalone script this AC's own evidence comes from (e.g.
    #: `verify_uc1_data_surface.py`, which is exactly §10.4's manual table
    #: turned into something CI runs) — `[sys.executable, "scripts/....py"]`.
    script: tuple[str, ...] | None = None
    #: Set only for AC-8/AC-9/AC-10: why nothing in this repo can answer this
    #: one without a human, and what evidence they need to supply.
    manual_note: str | None = None


CHECKS: tuple[ACCheck, ...] = (
    ACCheck(
        id="AC-1",
        title="Tạo được agent UC1 từ một câu mô tả tiếng Việt",
        verification="Chạy tay end-to-end",
        pytest_targets=(
            "tests/test_api/test_uc1_demo_rehearsal.py::test_full_demo_script_runs_within_the_eight_minute_budget",
        ),
    ),
    ACCheck(
        id="AC-2",
        title="sales_rep và sales_manager hỏi cùng câu nhận kết quả khác nhau",
        verification="Hai phiên song song",
        pytest_targets=(
            "tests/test_api/test_uc1_demo_rehearsal.py::test_full_demo_script_runs_within_the_eight_minute_budget",
            "tests/test_db/test_uc1_erp_query.py",
        ),
    ),
    ACCheck(
        id="AC-3",
        title="Không hành động write nào thực thi mà thiếu approval hợp lệ",
        verification="opa test + kiểm tra hàng chờ",
        pytest_targets=(
            "tests/test_compiler/test_pep_gates_every_catalog_tool.py",
            "tests/test_api/test_uc1_action_approval_routes.py",
        ),
        script=("opa", "test", "docs/usecase_spec/platform/"),
    ),
    ACCheck(
        id="AC-4",
        title="Prompt injection không kích hoạt được tool write",
        verification="3 case trong golden set",
        pytest_targets=(
            "tests/test_compiler/test_provenance.py",
            "tests/test_api/test_uc1_deployed_chat_routes.py::test_a_write_tool_call_following_a_read_in_the_same_turn_is_denied",
            "tests/test_evals/test_uc1_golden_set.py",
        ),
    ),
    ACCheck(
        id="AC-5",
        title="Không cột nào trong never_expose xuất hiện trong câu trả lời",
        verification="leak_check",
        pytest_targets=("tests/test_evals/test_uc1_rule_checker.py",),
    ),
    ACCheck(
        id="AC-6",
        title="Agent role không SELECT được fin_invoice",
        verification="Chạy SQL trực tiếp",
        script=(sys.executable, "scripts/verify_uc1_data_surface.py"),
    ),
    ACCheck(
        id="AC-7",
        title="v_audit_integrity trả rỗng; sau khi sửa log thì trả ra dòng gãy",
        verification="Chạy SQL trực tiếp",
        pytest_targets=("tests/test_db/test_uc1_audit_log.py",),
        script=(sys.executable, "scripts/verify_uc1_data_surface.py"),
    ),
    ACCheck(
        id="AC-8",
        title="Container agent không resolve được domain bên ngoài",
        verification="docker exec ... nslookup",
        manual_note=(
            "Cần một agent đã deploy thật đang chạy trong container riêng của nó "
            "(src/agentspec/network_policy.py sinh NetworkPolicy lúc deploy). Chạy tay: "
            "`docker exec <container_agent> nslookup <domain ngoài allowlist>` → kỳ vọng "
            "không resolve được. Dán nguyên văn output vào --manual-evidence."
        ),
    ),
    ACCheck(
        id="AC-9",
        title="DeepEval đạt ≥ 90% trên golden set",
        verification="CI",
        manual_note=(
            "Mọi test tự động trong repo này chạy golden set với model giả có kịch bản "
            "trước (tests/conftest.py's scripted_chat_model_factory) — đúng chủ đích, để "
            "không flaky/không tốn gọi LLM thật. Con số ≥90% thật cần chạy "
            "`POST /agents/{id}/versions/{v}/golden-set/run` với model thật (hoặc job CI "
            "riêng cấu hình model thật) rồi dán pass_rate vào --manual-evidence."
        ),
    ),
    ACCheck(
        id="AC-10",
        title="Cài từ máy trắng dưới 15 phút",
        verification="Bấm giờ",
        manual_note=(
            "Bấm giờ thật trên một máy sạch theo RUN_GUIDE.md, từ lúc bắt đầu tới lúc "
            "`make run`/uvicorn phục vụ request đầu tiên thành công. Dán thời gian đo được "
            "vào --manual-evidence."
        ),
    ),
    ACCheck(
        id="AC-11",
        title="Xin duyệt (bước 8) bị chặn nếu chưa có ít nhất một lượt chat thử (bước 7)",
        verification="Gọi API xin duyệt trên version chưa có sandbox run — kỳ vọng lỗi có cấu trúc",
        pytest_targets=(
            "tests/test_api/test_uc1_approval_routes.py::test_submitting_a_version_with_no_sandbox_run_is_refused_with_a_structured_error",
        ),
    ),
    ACCheck(
        id="AC-12",
        title="Hồ sơ duyệt hiển thị đúng transcript sandbox (node, tool, chi phí) của lượt chat thử gần nhất",
        verification="Đối chiếu tay giữa transcript và hồ sơ hiển thị cho admin",
        pytest_targets=(
            "tests/test_api/test_uc1_queue_diff_dossier_routes.py::test_dossier_embeds_the_latest_sandbox_transcript_and_eval_run",
            "tests/test_api/test_uc1_demo_rehearsal.py::test_full_demo_script_runs_within_the_eight_minute_budget",
        ),
    ),
)


@dataclass(slots=True)
class Evidence:
    command: str
    exit_code: int
    output_tail: str


@dataclass(slots=True)
class CheckResult:
    check: ACCheck
    status: str  # "PASS" | "FAIL" | "PENDING"
    started_at: datetime
    evidence: list[Evidence] = field(default_factory=list)
    manual_evidence: str | None = None


def _run(command: tuple[str, ...]) -> Evidence:
    try:
        completed = subprocess.run(  # noqa: S603 — every command here is this script's own fixed table, never caller input
            command, cwd=_REPO_ROOT, capture_output=True, text=True, timeout=600
        )
        output = (completed.stdout + completed.stderr).strip()
        exit_code = completed.returncode
    except FileNotFoundError as exc:
        output = f"lệnh không tìm thấy: {exc}"
        exit_code = 127
    except subprocess.TimeoutExpired:
        output = "hết thời gian chờ (600s)"
        exit_code = 124
    tail = "\n".join(output.splitlines()[-_OUTPUT_TAIL_LINES:])
    return Evidence(command=" ".join(command), exit_code=exit_code, output_tail=tail)


def _run_check(check: ACCheck, *, manual_evidence: dict[str, str]) -> CheckResult:
    started_at = datetime.now(UTC)
    result = CheckResult(check=check, status="PASS", started_at=started_at)

    if check.pytest_targets:
        evidence = _run((sys.executable, "-m", "pytest", *check.pytest_targets, "-q"))
        result.evidence.append(evidence)
        if evidence.exit_code != 0:
            result.status = "FAIL"

    if check.script is not None:
        evidence = _run(check.script)
        result.evidence.append(evidence)
        # `opa` missing locally is a setup gap, not this AC failing — recorded
        # as PENDING (needs a human with `opa` installed) rather than FAIL, so
        # a machine without the OPA CLI does not report a false red AC-3.
        if evidence.exit_code == 127 and check.script[0] == "opa":
            result.status = "PENDING" if result.status == "PASS" else result.status
        elif evidence.exit_code != 0:
            result.status = "FAIL"

    if check.manual_note is not None:
        supplied = manual_evidence.get(check.id)
        if supplied:
            result.manual_evidence = supplied
        elif result.status == "PASS":
            result.status = "PENDING"

    return result


def _render_report(results: list[CheckResult]) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    passed = sum(1 for r in results if r.status == "PASS")
    lines = [
        "# UC1-37 (#149) — Checklist AC-1..AC-12",
        "",
        f"Chạy lúc: {now}",
        f"Kết quả: {passed}/{len(results)} AC xanh",
        "",
    ]
    for r in results:
        icon = {"PASS": "✅", "FAIL": "❌", "PENDING": "⏳"}[r.status]
        lines.append(f"## {icon} {r.check.id} — {r.check.title}")
        lines.append("")
        lines.append(f"- Cách kiểm chứng: {r.check.verification}")
        lines.append(f"- Trạng thái: **{r.status}**")
        lines.append(f"- Thời gian: {r.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        for evidence in r.evidence:
            lines.append(f"- Lệnh: `{evidence.command}` (exit {evidence.exit_code})")
            if evidence.output_tail:
                lines.append("  ```")
                lines.extend(f"  {line}" for line in evidence.output_tail.splitlines())
                lines.append("  ```")
        if r.check.manual_note is not None:
            if r.manual_evidence:
                lines.append(f"- Bằng chứng thủ công: {r.manual_evidence}")
            else:
                lines.append(f"- Cần bằng chứng thủ công: {r.check.manual_note}")
        lines.append("")
    return "\n".join(lines)


def _parse_manual_evidence(pairs: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--manual-evidence expects AC-ID=text, got {pair!r}")
        ac_id, _, text = pair.partition("=")
        parsed[ac_id.strip()] = text.strip()
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--manual-evidence",
        action="append",
        default=[],
        metavar="AC-ID=text",
        help="Evidence for a manual AC (AC-8/AC-9/AC-10) a human already ran. Repeatable.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"Report path (default: {_DEFAULT_EVIDENCE_DIR}/<timestamp>.md)",
    )
    args = parser.parse_args()

    manual_evidence = _parse_manual_evidence(args.manual_evidence)

    results = [_run_check(check, manual_evidence=manual_evidence) for check in CHECKS]

    report = _render_report(results)
    out_path = args.out or (_DEFAULT_EVIDENCE_DIR / f"{datetime.now(UTC).strftime('%Y-%m-%dT%H-%M-%SZ')}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    print(report)
    print(f"\nBáo cáo đã lưu: {out_path}")

    all_green = all(r.status == "PASS" for r in results)
    return 0 if all_green else 1


if __name__ == "__main__":
    sys.exit(main())
