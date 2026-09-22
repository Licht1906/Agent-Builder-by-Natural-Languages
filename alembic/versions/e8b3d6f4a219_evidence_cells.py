"""evidence_cells — the scenario grid's one stored result per cell

Revision ID: e8b3d6f4a219
Revises: d2f7a93c1b56
Create Date: 2026-08-23 12:00:00.000000

ADR-0044 §1 / ADR-0047 §2, imported into this branch by ADR-0054 §3 (ticket
EVAL-4 / #154). One row per (version, cell, document hash, environment
fingerprint); the four-column UNIQUE is the invalidation mechanism — a changed
document or environment produces a different key, so old evidence stops
matching without any job marking it stale. `eval_case_id` points at the suite
case the cell IS (`origin=grid`, ADR-0042 §2); `sandbox_run_ids` points at the
trials' transcripts in `sandbox_runs` rather than copying them (ADR-0044 §5).

No `certifications` table and no `system_config` change ride along: ADR-0054
§3 keeps the zone thresholds as pre-registered constants
(`src/evals/thresholds.py`) and §7 defers the envelope/certificate (EVAL-8).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8b3d6f4a219"
down_revision: str | None = "d2f7a93c1b56"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evidence_cells",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "version_id",
            sa.Integer(),
            sa.ForeignKey("agent_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cell_key", sa.String(), nullable=False),
        sa.Column("spec_hash", sa.String(), nullable=False),
        sa.Column("env_fingerprint", sa.String(), nullable=False),
        sa.Column("zone", sa.String(), nullable=False),
        sa.Column(
            "eval_case_id",
            sa.Integer(),
            sa.ForeignKey("eval_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("k", sa.Integer(), nullable=False),
        sa.Column("wilson_lower", sa.Float(), nullable=False),
        sa.Column("wilson_upper", sa.Float(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("rule_failures", sa.JSON(), nullable=False),
        sa.Column("sandbox_run_ids", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "version_id", "cell_key", "spec_hash", "env_fingerprint", name="uq_evidence_cells_identity"
        ),
    )
    op.create_index("ix_evidence_cells_version_id", "evidence_cells", ["version_id"])
    op.create_index("ix_evidence_cells_cell_key", "evidence_cells", ["cell_key"])
    op.create_index("ix_evidence_cells_eval_case_id", "evidence_cells", ["eval_case_id"])


def downgrade() -> None:
    op.drop_index("ix_evidence_cells_eval_case_id", table_name="evidence_cells")
    op.drop_index("ix_evidence_cells_cell_key", table_name="evidence_cells")
    op.drop_index("ix_evidence_cells_version_id", table_name="evidence_cells")
    op.drop_table("evidence_cells")
