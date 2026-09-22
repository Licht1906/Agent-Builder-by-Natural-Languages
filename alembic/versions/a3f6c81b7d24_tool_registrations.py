"""tool registrations

Revision ID: a3f6c81b7d24
Revises: c9d2f4a17b60
Create Date: 2026-08-10 10:00:00.000000

The DB half of `TOOL_DEFINITIONS` (ticket #52, ADR-0023 §1, §7) — creator-registered
MCP connections awaiting admin review, the same registry/DB split
`template_submissions` (#53) drew for the template catalog. Only ever
`source: creator_registered` (the built-in connections stay code), so there is no
`source` column, only the `status` a row moves through: `pending` on arrival, then
`approved`/`rejected` by an admin, and `disabled` afterwards.

`actions` is a verbatim copy of the MCP server's own `tools/list` answer, taken
once at registration. `risk_overrides` and `mock_response_examples` sit beside it
keyed by action name rather than inside its entries, so a review never edits the
introspected schema — which is exactly the line ADR-0023 §7 draws between what an
admin may change (risk) and what only the server may say (the schema).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f6c81b7d24"
down_revision: str | Sequence[str] | None = "c9d2f4a17b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `tool_registrations`."""
    op.create_table(
        "tool_registrations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("submitted_by", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("server_url", sa.String(), nullable=False),
        sa.Column(
            "credential_kind",
            sa.Enum("NONE", "OAUTH_PER_USER", "SECRET_REFERENCE", name="credentialkind"),
            nullable=False,
        ),
        sa.Column("credential_provider", sa.String(), nullable=False),
        sa.Column("credential_scopes", sa.JSON(), nullable=False),
        # A *reference* into the secret store, never the secret: the Publish
        # security requirement keeps credentials in the backend store, and a
        # column holding one would put every creator's token in this table.
        sa.Column("credential_secret_ref", sa.String(), nullable=False),
        sa.Column("actions", sa.JSON(), nullable=False),
        sa.Column("risk_overrides", sa.JSON(), nullable=False),
        sa.Column("mock_response_examples", sa.JSON(), nullable=False),
        sa.Column(
            "usage_scope",
            sa.Enum("PUBLIC", "ORG_RESTRICTED", "CREATOR_ONLY", name="usagescope"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("PENDING", "APPROVED", "REJECTED", "DISABLED", name="toolstatus"),
            nullable=False,
        ),
        sa.Column("decided_by", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("rejection_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        # No cascade on either user column: who registered a connection and who
        # decided on it are audit information about a governance decision — the
        # same call `template_submissions` and `approval_decisions` make.
        sa.ForeignKeyConstraint(["submitted_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tool_registrations_submitted_by"), "tool_registrations", ["submitted_by"])


def downgrade() -> None:
    """Drop `tool_registrations` and the three enum types it owns."""
    op.drop_index(op.f("ix_tool_registrations_submitted_by"), table_name="tool_registrations")
    op.drop_table("tool_registrations")
    # Postgres ENUM types outlive their column — drop_table doesn't drop them.
    for enum in ("toolstatus", "usagescope", "credentialkind"):
        sa.Enum(name=enum).drop(op.get_bind(), checkfirst=True)
