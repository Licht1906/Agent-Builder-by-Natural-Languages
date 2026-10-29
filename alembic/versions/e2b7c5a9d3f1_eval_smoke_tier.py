"""eval smoke tier — run kind and the smoke-only verdict cache

Revision ID: e2b7c5a9d3f1
Revises: d9a6e3b1f4c7
Create Date: 2026-10-30 12:00:00.000000

EV-04 / ADR-0069 §7: a run is `smoke` or `gate`; every run before this was a
gate run. `eval_smoke_cache` holds one reusable smoke verdict per case.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2b7c5a9d3f1"
down_revision: str | Sequence[str] | None = "d9a6e3b1f4c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND = sa.Enum("SMOKE", "GATE", name="evalrunkind")


def upgrade() -> None:
    _KIND.create(op.get_bind(), checkfirst=True)
    op.add_column("eval_runs", sa.Column("kind", _KIND, nullable=False, server_default="GATE"))
    op.create_table(
        "eval_smoke_cache",
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("eval_cases.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("path", sa.JSON(), nullable=False),
        sa.Column("path_hash", sa.String(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("eval_smoke_cache")
    op.drop_column("eval_runs", "kind")
    _KIND.drop(op.get_bind(), checkfirst=True)
