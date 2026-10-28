"""The package the CI smoke test runs (DS-19): a minimal approved agent, assembled exactly as the export does.

    PYTHONPATH=. python scripts/build_smoke_package.py out/package.zip

No database is read: `assemble_package` is the export's pure half, so the zip
is the same files the "Xuất gói chạy độc lập" button writes for an agent
with one answering node. `scripts/smoke_package.sh` then unpacks it, fills in
its secrets and runs `docker compose up`.
"""

import sys
from pathlib import Path

from src.agents.package_export import assemble_package
from src.agents.portability import BUNDLE_FORMAT

BUNDLE = {
    "format": BUNDLE_FORMAT,
    "name": "Tro ly kiem thu goi",
    "source_version_id": 1,
    "solution_spec": {
        "goal": "Trả lời câu hỏi chung của nhân viên",
        "users": ["Nhân viên"],
        "data": [{"name": "Sổ tay", "kind": "uploaded_file", "ref": "so-tay.pdf"}],
        "actions": ["Trả lời"],
        "expected_outcome": ["Một câu trả lời"],
        "success_criteria": [{"text": "Trả lời đúng câu hỏi", "category": "correctness"}],
        "prohibitions": ["Không bịa"],
    },
    "workflow": {
        "start_node_id": "answer",
        "nodes": [
            {"id": "answer", "type": "agent_llm", "config": {"prompt": "Trả lời ngắn gọn.", "model": "gpt-4o-mini"}},
            {"id": "finish", "type": "end", "config": {}},
        ],
        "edges": [{"id": "e1", "source_node_id": "answer", "source_port": "done", "target_node_id": "finish"}],
        "metadata": {},
    },
    "database_schema": {},
}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("cách dùng: build_smoke_package.py <out.zip>", file=sys.stderr)
        return 2
    target = assemble_package(Path(argv[1]), bundle=BUNDLE, sources=[])
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
