"""run approvals

Revision ID: a1c7d5e40b93
Revises: c4f7a91d3e28
Create Date: 2026-08-09 10:00:00.000000

The ledger behind Human Approval pause/resume (ticket #47, ADR-0024) — the
queryable half of a paused run. The checkpointer already holds *where* a run
stopped, but a checkpoint answers no question anybody can ask it in SQL; this
table is what a reviewer's list pages through and what survives whatever
LangGraph does to old checkpoints.

Keyed `(run_id, node_id, occurrence_index)` and append-only (ADR-0024 §3): a
`HumanApproval` node inside a `While` body pauses once per pass, so a run that
goes round the loop twice accumulates rows rather than overwriting one. The
unique index makes that key a rule the database keeps rather than a convention
the writer remembers — and it is what makes the node's second execution, when
`interrupt()` replays it on resume, find its row instead of inserting a twin.

The columns past `reason` are what the *pause* has to remember. A run stops in
the middle of an HTTP response that is already closing, so the thread address,
the input, the transcript so far and the ledger at that moment all have to
survive here or the resumed half cannot produce one complete run's worth of
evidence — and `elapsed_seconds` in particular is what lets ADR-0024 §7 exclude
time spent waiting on a person from `max_wall_seconds`.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c7d5e40b93"
down_revision: str | Sequence[str] | None = "c4f7a91d3e28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `run_approvals`."""
    op.create_table(
        "run_approvals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("occurrence_index", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("sandbox", sa.Boolean(), nullable=False),
        sa.Column("trace_id", sa.String(), nullable=False),
        sa.Column("approver_role", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("input_message", sa.String(), nullable=False),
        sa.Column("status", sa.Enum("PENDING", "APPROVED", "REJECTED", name="runapprovalstatus"), nullable=False),
        sa.Column("decided_by", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("transcript", sa.JSON(), nullable=False),
        sa.Column("elapsed_seconds", sa.Float(), nullable=False),
        sa.Column("usage_by_model", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        # Both cascade for `sandbox_runs`' reason: a paused run is a statement
        # about one version of one agent, and it says nothing once either is
        # gone. `decided_by` deliberately does not — an audit row naming an
        # account that no longer exists is better than one with nobody on it.
        sa.ForeignKeyConstraint(["version_id"], ["agent_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_run_approvals_run_id"), "run_approvals", ["run_id"])
    op.create_index(op.f("ix_run_approvals_version_id"), "run_approvals", ["version_id"])
    op.create_index(op.f("ix_run_approvals_agent_id"), "run_approvals", ["agent_id"])
    # The key ADR-0024 §3 names, enforced rather than assumed: the node writes
    # its row on first execution and looks the same row up when `interrupt()`
    # replays it, so a second insert under one key would be a duplicate pause.
    op.create_index(
        "uq_run_approvals_occurrence",
        "run_approvals",
        ["run_id", "node_id", "occurrence_index"],
        unique=True,
    )


def downgrade() -> None:
    """Drop `run_approvals` and the enum type it owns."""
    op.drop_index("uq_run_approvals_occurrence", table_name="run_approvals")
    op.drop_index(op.f("ix_run_approvals_agent_id"), table_name="run_approvals")
    op.drop_index(op.f("ix_run_approvals_version_id"), table_name="run_approvals")
    op.drop_index(op.f("ix_run_approvals_run_id"), table_name="run_approvals")
    op.drop_table("run_approvals")
    # Postgres ENUM types outlive their column — drop_table doesn't drop them.
    sa.Enum(name="runapprovalstatus").drop(op.get_bind(), checkfirst=True)
