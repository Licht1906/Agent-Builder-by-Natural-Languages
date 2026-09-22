"""template submissions

Revision ID: b7e4d2f819a3
Revises: e7d2b40a9c15
Create Date: 2026-08-09 20:00:00.000000

The DB half of `TEMPLATE_REGISTRY` (ticket #53, ADR-0025 §6-8) — creator "save as
template" candidates awaiting admin review, the same registry/DB split
`ToolDefinition` (#47) already established for connections. Only ever
`source: creator_submitted` (the built-in seed stays code), so there is no
`source` column, only the `status` a row moves through: `pending` on arrival,
then `approved`/`rejected`/`disabled` by an admin.

`nodes`/`edges`/`start_node_id` are a literal, stripped copy of a
`workflow_confirmed` version's graph, taken once at submission time — never a
live reference. That is what lets an admin reject or disable a row without
touching any `WorkflowJSON` a Workflow Generator run already produced from it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e4d2f819a3"
down_revision: str | Sequence[str] | None = "e7d2b40a9c15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `template_submissions`."""
    op.create_table(
        "template_submissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("submitted_by", sa.Integer(), nullable=False),
        sa.Column("source_version_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("nodes", sa.JSON(), nullable=False),
        sa.Column("edges", sa.JSON(), nullable=False),
        sa.Column("start_node_id", sa.String(), nullable=False),
        sa.Column("required_data_kinds", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "APPROVED", "REJECTED", "DISABLED", name="templatestatus"),
            nullable=False,
        ),
        sa.Column("decided_by", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("rejection_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        # No cascade on `submitted_by`: a submission is under review by an admin
        # who is not the submitting creator, and the row is audit information —
        # the same call `ApprovalDecision.admin_id` makes.
        sa.ForeignKeyConstraint(["submitted_by"], ["users.id"]),
        # `SET NULL`, not cascade: the submission is the creator's own artifact
        # and must survive the source agent/version being deleted later — the
        # column is provenance, not a live reference the copy depends on.
        sa.ForeignKeyConstraint(["source_version_id"], ["agent_versions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_template_submissions_submitted_by"), "template_submissions", ["submitted_by"])


def downgrade() -> None:
    """Drop `template_submissions` and the enum type it owns."""
    op.drop_index(op.f("ix_template_submissions_submitted_by"), table_name="template_submissions")
    op.drop_table("template_submissions")
    # Postgres ENUM types outlive their column — drop_table doesn't drop them.
    sa.Enum(name="templatestatus").drop(op.get_bind(), checkfirst=True)
