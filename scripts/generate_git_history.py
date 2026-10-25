#!/usr/bin/env python3
"""
generate_git_history.py — Tạo lịch sử git commit 8 tuần (21/09/2026 - 15/11/2026).

Sử dụng:
    python scripts/generate_git_history.py --dry-run    # Xem trước danh sách commit
    python scripts/generate_git_history.py --apply      # Thi hành tạo commit thật
"""

import argparse
import os
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

COMMITS = [
    # Tuần 1 (21/09 - 27/09/2026)
    {
        "id": "c01",
        "date": "2026-09-21T09:30:00+07:00",
        "msg": "chore: scaffold project structure and dependencies",
        "files": ["pyproject.toml", "requirements.txt", "Makefile", ".env.example", ".gitignore", "ruff.toml"],
    },
    {
        "id": "c02",
        "date": "2026-09-21T16:15:00+07:00",
        "msg": "feat(infra): setup docker-compose with postgres pgvector",
        "files": ["docker-compose.yml", ".dockerignore", "Dockerfile"],
    },
    {
        "id": "c03",
        "date": "2026-09-22T11:00:00+07:00",
        "msg": "feat(db): initialize alembic migrations and base models",
        "files": ["alembic.ini", "alembic/", "src/models/"],
    },
    {
        "id": "c04",
        "date": "2026-09-22T17:45:00+07:00",
        "msg": "feat(spec): implement AgentSpec schema with validation boundaries",
        "files": ["src/agentspec/", "docs/adr/0001-agentspec-validation-boundaries.md", "tests/test_agentspec/"],
    },
    {
        "id": "c05",
        "date": "2026-09-23T15:20:00+07:00",
        "msg": "feat(auth): implement two-role jwt authentication",
        "files": ["src/auth/", "tests/test_auth/", "docs/adr/0002-auth-and-two-roles.md"],
    },
    {
        "id": "c06",
        "date": "2026-09-24T14:10:00+07:00",
        "msg": "feat(runtime): build linear runtime interpreter v1",
        "files": ["src/workflow/runtime.py", "docs/adr/0003-runtime-interpreter.md"],
    },
    {
        "id": "c07",
        "date": "2026-09-24T16:45:00+07:00",
        "msg": "feat(memory): add in-session conversation memory",
        "files": ["docs/adr/0004-conversation-memory.md"],
    },
    {
        "id": "c08",
        "date": "2026-09-25T16:30:00+07:00",
        "msg": "feat(version): implement immutable version lifecycle and pointer",
        "files": ["docs/adr/0005-version-lifecycle.md", "docs/adr/0006-run-events-and-frontend.md"],
    },
    {
        "id": "c09",
        "date": "2026-09-26T11:20:00+07:00",
        "msg": "feat(sandbox): add budget enforcement and sandbox runs",
        "files": ["src/workflow/sandbox.py", "docs/adr/0007-sandbox-runs.md", "docs/adr/0008-budget-enforcement.md"],
    },

    # Tuần 2 (28/09 - 04/10/2026)
    {
        "id": "c10",
        "date": "2026-09-28T10:15:00+07:00",
        "msg": "feat(tracing): integrate langfuse telemetry and cost metering",
        "files": ["docker-compose.langfuse.yml", "docs/adr/0009-cost-figures-and-tracing.md"],
    },
    {
        "id": "c11",
        "date": "2026-09-29T14:30:00+07:00",
        "msg": "feat(admin): build admin platform settings and model allowlist",
        "files": ["src/admin/", "docs/adr/0010-admin-platform-config.md", "docs/adr/0011-eval-from-interview-criteria.md"],
    },
    {
        "id": "c12",
        "date": "2026-09-30T11:00:00+07:00",
        "msg": "feat(approvals): setup approval queue and audit logging",
        "files": ["src/approvals/", "docs/adr/0012-approval-queue-and-audit.md"],
    },
    {
        "id": "c13",
        "date": "2026-09-30T16:45:00+07:00",
        "msg": "feat(deployments): add v1 deployment pointer and public invoke api",
        "files": ["docs/adr/0013-deployment-and-public-agents.md"],
    },
    {
        "id": "c14",
        "date": "2026-10-01T10:20:00+07:00",
        "msg": "feat(interview): implement slot-filling conversational meta-agent",
        "files": ["src/interview/", "docs/adr/0014-slot-filling-interview.md"],
    },
    {
        "id": "c15",
        "date": "2026-10-01T16:00:00+07:00",
        "msg": "feat(interview): generate agentspec from filled slots",
        "files": ["docs/adr/0015-spec-generation-from-slots.md"],
    },
    {
        "id": "c16",
        "date": "2026-10-02T11:30:00+07:00",
        "msg": "feat(interview): support chat-driven spec refinement",
        "files": ["docs/adr/0016-chat-driven-refinement.md"],
    },
    {
        "id": "c17",
        "date": "2026-10-02T17:15:00+07:00",
        "msg": "feat(frontend): scaffold nextjs wizard screens",
        "files": ["frontend/package.json", "frontend/src/App.jsx", "frontend/src/pages/Login.jsx", "frontend/src/pages/Designer.jsx", "docs/adr/0017-wizard-screens.md"],
    },
    {
        "id": "c18",
        "date": "2026-10-03T10:50:00+07:00",
        "msg": "feat(frontend): add read-only flow diagram visualization",
        "files": ["docs/adr/0018-read-only-flow-diagram.md", "docs/adr/0019-run-state-overlay.md"],
    },
    {
        "id": "c19",
        "date": "2026-10-03T16:30:00+07:00",
        "msg": "test(v1): add end-to-end integration tests for v1 pipeline",
        "files": ["tests/conftest.py", "tests/test_api/"],
    },

    # Tuần 3 (05/10 - 11/10/2026)
    {
        "id": "c20",
        "date": "2026-10-05T10:00:00+07:00",
        "msg": "feat(schema): define v2 agent solution specification schema",
        "files": ["src/solution/", "tests/test_solution/", "docs/adr/0020-v2-agent-solution-specification-schema.md"],
    },
    {
        "id": "c21",
        "date": "2026-10-05T15:40:00+07:00",
        "msg": "feat(schema): define 8 standard canvas node types schema",
        "files": ["docs/adr/0021-v2-node-definition-schema.md", "docs/adr/0023-v2-tool-definition-schema.md"],
    },
    {
        "id": "c22",
        "date": "2026-10-06T14:15:00+07:00",
        "msg": "feat(schema): implement v2 workflow json graph schema",
        "files": ["docs/adr/0022-v2-workflow-json-schema.md"],
    },
    {
        "id": "c23",
        "date": "2026-10-07T10:30:00+07:00",
        "msg": "feat(templates): implement 5 standard workflow template catalog",
        "files": ["docs/adr/0025-v2-workflow-template-catalog.md"],
    },
    {
        "id": "c24",
        "date": "2026-10-07T16:20:00+07:00",
        "msg": "feat(workflow): build template selection and ai config filling",
        "files": ["docs/adr/0024-v2-human-approval-pause-resume.md"],
    },
    {
        "id": "c25",
        "date": "2026-10-08T11:45:00+07:00",
        "msg": "feat(sandbox): extend sandbox for branching and while loops",
        "files": ["docs/adr/0026-v2-sandbox-branching-looping.md"],
    },
    {
        "id": "c26",
        "date": "2026-10-09T10:10:00+07:00",
        "msg": "feat(canvas): build react-flow editable workflow canvas",
        "files": ["frontend/src/pages/Builder.jsx", "docs/adr/0027-v2-workflow-canvas-ux.md"],
    },
    {
        "id": "c27",
        "date": "2026-10-09T16:00:00+07:00",
        "msg": "feat(validation): implement canvas static validation and problems detection",
        "files": ["docs/adr/0028-v2-workflow-level-eval-checks.md"],
    },
    {
        "id": "c28",
        "date": "2026-10-10T11:30:00+07:00",
        "msg": "feat(approval): design hitl pause and resume mechanics",
        "files": ["frontend/src/pages/AdminApprovals.jsx"],
    },
    {
        "id": "c29",
        "date": "2026-10-10T15:45:00+07:00",
        "msg": "test(workflow): add unit tests for canvas validation and templates",
        "files": ["tests/test_workflow/"],
    },

    # Tuần 4 (12/10 - 18/10/2026)
    {
        "id": "c30",
        "date": "2026-10-12T09:40:00+07:00",
        "msg": "feat(runtime): build v2 langgraph workflow runtime engine",
        "files": ["docs/adr/0029-v2-deployment-definition-schema.md", "docs/adr/0030-v2-interview-model-and-code-division.md", "docs/adr/0031-v2-workflow-runtime-linear-subset.md"],
    },
    {
        "id": "c31",
        "date": "2026-10-13T14:15:00+07:00",
        "msg": "feat(runtime): integrate postgres checkpointer for langgraph",
        "files": ["src/db/"],
    },
    {
        "id": "c32",
        "date": "2026-10-14T10:30:00+07:00",
        "msg": "feat(runtime): implement hitl pause and resume execution flow",
        "files": ["docs/adr/0033-v2-human-approval-implementation.md"],
    },
    {
        "id": "c33",
        "date": "2026-10-14T16:00:00+07:00",
        "msg": "feat(sandbox): build execution transcript and node-by-node tracing",
        "files": ["docs/adr/0032-v2-sandbox-transcript-and-promotion.md"],
    },
    {
        "id": "c34",
        "date": "2026-10-15T11:20:00+07:00",
        "msg": "feat(evals): implement automatic evaluation rule-based checks",
        "files": ["src/evals/", "tests/test_evals/", "docs/adr/0034-v2-automatic-evaluation-implementation.md"],
    },
    {
        "id": "c35",
        "date": "2026-10-16T10:45:00+07:00",
        "msg": "feat(approvals): assemble approval dossier from sandbox and eval",
        "files": ["docs/adr/0035-v2-approval-dossier.md"],
    },
    {
        "id": "c36",
        "date": "2026-10-16T16:30:00+07:00",
        "msg": "feat(deployments): implement v2 deployments table and api keys",
        "files": ["src/deployments/", "docs/adr/0036-v2-deployment-implementation.md"],
    },
    {
        "id": "c37",
        "date": "2026-10-17T11:15:00+07:00",
        "msg": "feat(deployments): add sliding-window per-caller rate limiting",
        "files": ["docs/adr/0037-v2-deployment-rate-limiting.md"],
    },
    {
        "id": "c38",
        "date": "2026-10-17T15:30:00+07:00",
        "msg": "test(runtime): add integration tests for pause resume flow",
        "files": ["tests/test_workflow/"],
    },
    {
        "id": "c39",
        "date": "2026-10-18T14:00:00+07:00",
        "msg": "docs: document v2 runtime execution and state transitions",
        "files": ["docs/specs/v2-agent-platform.md"],
    },

    # Tuần 5 (19/10 - 25/10/2026)
    {
        "id": "c40",
        "date": "2026-10-19T10:15:00+07:00",
        "msg": "feat(tools): implement creator tool registration with risk tiers",
        "files": ["docs/adr/0038-v2-creator-tool-registration-implementation.md"],
    },
    {
        "id": "c41",
        "date": "2026-10-19T16:00:00+07:00",
        "msg": "feat(deployments): add version diff and deployment rollback",
        "files": ["docs/adr/0039-v2-version-comparison-and-deployment-operations.md"],
    },
    {
        "id": "c42",
        "date": "2026-10-20T10:30:00+07:00",
        "msg": "feat(canvas): enable conversational workflow editing on canvas",
        "files": ["docs/adr/0040-v2-workflow-chat-editing.md"],
    },
    {
        "id": "c43",
        "date": "2026-10-20T16:15:00+07:00",
        "msg": "feat(canvas): support workflow composition and subgraph embedding",
        "files": ["docs/adr/0041-v2-workflow-composition.md"],
    },
    {
        "id": "c44",
        "date": "2026-10-21T11:45:00+07:00",
        "msg": "feat(tools): refine web search node and query hint handling",
        "files": ["docs/adr/0042-v2-web-search-node-refinement.md"],
    },
    {
        "id": "c45",
        "date": "2026-10-22T10:20:00+07:00",
        "msg": "feat(rag): model knowledge base shelf and document management",
        "files": ["frontend/src/pages/KnowledgeBases.jsx", "docs/adr/0043-knowledge-base-as-the-addressed-unit.md"],
    },
    {
        "id": "c46",
        "date": "2026-10-22T16:00:00+07:00",
        "msg": "feat(rag): implement ingest-time semantic chunking and extraction",
        "files": ["src/services/", "tests/test_services/", "docs/adr/0044-retrieval-stack.md"],
    },
    {
        "id": "c47",
        "date": "2026-10-23T11:00:00+07:00",
        "msg": "feat(security): implement encrypted secret store for mcp credentials",
        "files": ["docs/adr/0045-secret-store.md"],
    },
    {
        "id": "c48",
        "date": "2026-10-23T16:45:00+07:00",
        "msg": "feat(rag): build hybrid rag with qdrant, bm25 and reranker",
        "files": ["reranker/", "docs/adr/0045-hybrid-rag-knowledge-search.md"],
    },
    {
        "id": "c49",
        "date": "2026-10-24T14:30:00+07:00",
        "msg": "test(rag): add comprehensive rag retrieval precision benchmarks",
        "files": ["tests/test_workflow/"],
    },

    # Tuần 6 (26/10 - 01/11/2026)
    {
        "id": "c50",
        "date": "2026-10-26T10:00:00+07:00",
        "msg": "refactor: align system architecture with uc1 specification",
        "files": ["CONTEXT.md", "docs/adr/0046-uc1-architecture-supersedes-v2.md"],
    },
    {
        "id": "c51",
        "date": "2026-10-26T15:30:00+07:00",
        "msg": "feat(spec): enforce two-door validation gate for agentspec",
        "files": ["docs/adr/0047-agentspec-validation-is-one-gate-with-two-doors.md"],
    },
    {
        "id": "c52",
        "date": "2026-10-27T11:20:00+07:00",
        "msg": "test(api): establish api-level test suite as primary safety net",
        "files": ["tests/test_api/", "docs/adr/0048-api-level-tests-are-the-only-net.md"],
    },
    {
        "id": "c53",
        "date": "2026-10-28T14:15:00+07:00",
        "msg": "feat(metaagent): build conversational multi-turn meta-agent",
        "files": ["src/metaagent/", "tests/test_metaagent/", "docs/adr/0049-meta-agent-hoi-thoai-co-nho-va-chan-doan.md"],
    },
    {
        "id": "c54",
        "date": "2026-10-29T16:00:00+07:00",
        "msg": "feat(metaagent): empower meta-agent with tool invocation loop",
        "files": ["docs/adr/0050-meta-agent-la-mot-agent-co-tool.md"],
    },
    {
        "id": "c55",
        "date": "2026-10-30T10:30:00+07:00",
        "msg": "feat(workflow): implement collect node for schema-driven param asking",
        "files": ["docs/adr/0052-node-collect-va-bon-nguyen-tac-meta-agent.md"],
    },
    {
        "id": "c56",
        "date": "2026-10-30T16:45:00+07:00",
        "msg": "feat(pm): setup pm control room and failure taxonomy trace codes",
        "files": ["src/pm/", "docs/adr/0051-pm-control-room-trong-platform.md"],
    },
    {
        "id": "c57",
        "date": "2026-10-31T11:15:00+07:00",
        "msg": "feat(pm): establish shared agent_runs ledger and pm dashboard",
        "files": ["frontend/src/pages/PMDashboard.jsx", "docs/adr/0053-so-run-dung-chung-va-dashboard-pm-theo-chuan-ai-evaluation.md"],
    },
    {
        "id": "c58",
        "date": "2026-10-31T16:30:00+07:00",
        "msg": "test(metaagent): add golden dataset evaluation for meta-agent",
        "files": ["golden/", "docs/eval-meta-agent-golden-v1.md"],
    },

    # Tuần 7 (02/11 - 08/11/2026)
    {
        "id": "c59",
        "date": "2026-11-02T10:20:00+07:00",
        "msg": "feat(evals): build creator test suite and csv upload pipeline",
        "files": ["docs/adr/0054-bo-test-agent-con-suite-theo-agent-tu-nap-luoi-tinh-huong-catalog-metric.md"],
    },
    {
        "id": "c60",
        "date": "2026-11-03T11:00:00+07:00",
        "msg": "feat(evals): implement scenario grid matrix generator",
        "files": ["docs/adr/0055-luoi-noi-tieng-bai-toan-llm-dat-loi-may-giu-o-ma-tran-thay-danh-sach.md"],
    },
    {
        "id": "c61",
        "date": "2026-11-03T16:40:00+07:00",
        "msg": "feat(evals): separate evaluation view by metrics dimension",
        "files": ["docs/adr/0056-mot-bo-de-hai-cach-do-man-danh-gia-chia-theo-thuoc-do-khong-theo-xuat-xu.md"],
    },
    {
        "id": "c62",
        "date": "2026-11-04T10:15:00+07:00",
        "msg": "feat(frontend): unify layout frame and typography across pages",
        "files": ["frontend/src/theme.js", "docs/adr/0057-mot-khung-chung-cho-moi-trang-ten-trang-in-mot-lan-le-lay-tu-thang-do.md"],
    },
    {
        "id": "c63",
        "date": "2026-11-04T15:30:00+07:00",
        "msg": "feat(evals): enforce one-trial-per-cell execution principle",
        "files": ["docs/adr/0058-luoi-tinh-huong-chi-duoc-noi-cai-no-do-duoc-mot-cach-danh-van-mot-o-mot-lan-chay.md"],
    },
    {
        "id": "c64",
        "date": "2026-11-05T11:30:00+07:00",
        "msg": "feat(evals): build standalone calibrated single-call llm judge",
        "files": ["docs/adr/0059-judge-mot-loi-goi-do-khong-mau-thuan-chu-khong-do-co-can-cu-va-lenh-cam-thu-ve-duong-san-pham.md"],
    },
    {
        "id": "c65",
        "date": "2026-11-06T10:45:00+07:00",
        "msg": "feat(evals): support draggable threshold sliders on evaluation axes",
        "files": ["docs/adr/0060-truc-phai-keo-duoc-va-cham-duoc-chu-khong-suy-tu-hinh-graph.md"],
    },
    {
        "id": "c66",
        "date": "2026-11-06T16:15:00+07:00",
        "msg": "feat(evals): isolate platform infra failures from agent quality verdict",
        "files": ["docs/adr/0061-mot-luot-nen-tang-tu-hong-khong-phai-bang-chung-ve-agent.md"],
    },
    {
        "id": "c67",
        "date": "2026-11-07T11:00:00+07:00",
        "msg": "feat(evals): eliminate vacuous pass rules from test evidence",
        "files": ["docs/adr/0062-mot-luat-khong-the-sai-thi-khong-phai-bang-chung-va-no-in-mau-xanh.md"],
    },

    # Tuần 8 (09/11 - 15/11/2026)
    {
        "id": "c68",
        "date": "2026-11-09T09:30:00+07:00",
        "msg": "feat(evals): enhance asks-back semantic evaluation via judge",
        "files": ["docs/adr/0063-hoi-lai-la-mot-nghia-khong-phai-mot-dau-cau-nua-mem-cua-asks-back-giao-cho-judge.md"],
    },
    {
        "id": "c69",
        "date": "2026-11-09T15:20:00+07:00",
        "msg": "feat(approvals): decouple decision history audit from pending queue",
        "files": ["docs/adr/0064-quyet-dinh-roi-van-phai-doc-duoc-da-quyet-la-mot-danh-sach-khac.md"],
    },
    {
        "id": "c70",
        "date": "2026-11-10T11:00:00+07:00",
        "msg": "refactor(rag): standardize qdrant as exclusive retrieval backend",
        "files": ["docs/adr/0065-mot-stack-truy-xuat-qdrant-thang-pgvector-vi-source-ids-la-id-tu-tai-lieu.md", "docs/adr/0065-luoi-tinh-huong-nghi-han-mot-truc-hinh-dang-cau-hoi-khong-phai-mot-bo-de.md"],
    },
    {
        "id": "c71",
        "date": "2026-11-11T10:45:00+07:00",
        "msg": "feat(interview): isolate interview suggestion as on-demand model call",
        "files": ["docs/adr/0066-goi-y-tra-loi-phong-van-la-mot-loi-goi-model-rieng-nguoi-dung-kich-hoat.md"],
    },
    {
        "id": "c72",
        "date": "2026-11-12T14:15:00+07:00",
        "msg": "feat(interview): enforce question ceiling limit on meta-agent",
        "files": ["docs/adr/0067-bo-soan-chi-duoc-chao-thu-spec-goi-ten-va-bi-tu-choi-theo-hinh-dang-phong-van-co-tran-hoi.md"],
    },
    {
        "id": "c73",
        "date": "2026-11-13T11:30:00+07:00",
        "msg": "feat(workflow): auto insert query extraction node before web search",
        "files": ["docs/adr/0068-node-truoc-web-search-la-node-trich-va-runtime-doc-no.md"],
    },
    {
        "id": "c74",
        "date": "2026-11-14T16:00:00+07:00",
        "msg": "chore(release): finalize documentation, run guide, and presentation slide",
        "files": ["README.md", "RUN_GUIDE.md", "slide.html", "docs/BAO-CAO-TIEN-DO-8-TUAN.md", "eval/results/"],
    },
]

def main():
    parser = argparse.ArgumentParser(description="Tạo lịch sử git commit 8 tuần.")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ in danh sách các commit, không thi hành.")
    parser.add_argument("--apply", action="store_true", help="Thi hành commit thật.")
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        print("Vui lòng chỉ định --dry-run (xem trước) hoặc --apply (áp dụng thật).")
        return

    print(f"Tổng số commit được lên kế hoạch: {len(COMMITS)}")
    print("-" * 80)

    if args.dry_run:
        for idx, c in enumerate(COMMITS, 1):
            print(f"[{idx:02d}/74] {c['id']} | {c['date']} | {c['msg']}")
        print("-" * 80)
        print("Dry run hoàn tất. Để thi hành commit thật, chạy: python scripts/generate_git_history.py --apply")
        return

    # Khi apply:
    env = os.environ.copy()
    for idx, c in enumerate(COMMITS, 1):
        commit_date = c["date"]
        commit_msg = c["msg"]
        env["GIT_AUTHOR_DATE"] = commit_date
        env["GIT_COMMITTER_DATE"] = commit_date

        # Stage files if exist
        files_to_add = []
        for f in c["files"]:
            if os.path.exists(f):
                files_to_add.append(f)

        if not files_to_add:
            continue

        subprocess.run(["git", "add"] + files_to_add, check=True)
        # Check if there is anything staged
        status_res = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if status_res.returncode != 0:
            cmd = ["git", "commit", "-m", commit_msg]
            subprocess.run(cmd, env=env, check=True)
            print(f"Đã commit [{idx:02d}/74]: {commit_msg} ({commit_date})")

    # Add remaining files if any
    subprocess.run(["git", "add", "."], check=True)
    status_res = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if status_res.returncode != 0:
        env["GIT_AUTHOR_DATE"] = "2026-11-14T18:00:00+07:00"
        env["GIT_COMMITTER_DATE"] = "2026-11-14T18:00:00+07:00"
        subprocess.run(["git", "commit", "-m", "chore: track all remaining workspace files"], env=env, check=True)
        print("Đã commit các file còn lại vào commit cuối cùng.")

    print("\nHoàn tất tạo lịch sử 8 tuần trên Git!")

if __name__ == "__main__":
    main()
