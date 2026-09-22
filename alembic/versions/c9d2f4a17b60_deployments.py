"""deployments

Revision ID: c9d2f4a17b60
Revises: b7e4d2f819a3
Create Date: 2026-08-09 23:50:00.000000

Ticket #50 / ADR-0029. The deployed pointer moves off `agents.deployed_version_id`
and onto a row of its own, because the publication grew properties the agent has
no place for: whether it is serving, who may reach it, and the key that opens its
API door.

The column is **backfilled and then dropped**, not left behind as a mirror. Two
writable pointers is two answers to "which version is live", and the day they
disagree is the day a creator's screen and the runtime describe different agents.

Every backfilled row lands `active` — an agent that was serving keeps serving —
and `private`, which is the one behavioural break in this migration and is
deliberate: V1 had no access control at all, so "anyone signed in may chat" has
no equivalent under ADR-0029 §3, and the safe reading of a pointer set before the
question was asked is "the owner only". Widening it is one call the owner makes.

Their `api_key_hash` is the hash of a key nobody has ever seen. That is not a
placeholder: an unusable credential is exactly right for a Deployment nobody
minted a key for, and the owner's way to get one is the same "regenerate" button
that revokes a leaked one.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from src.db.models import DEFAULT_RATE_LIMIT_PER_USER
from src.deployments.keys import generate_api_key, hash_api_key

# revision identifiers, used by Alembic.
revision: str = "c9d2f4a17b60"
down_revision: str | Sequence[str] | None = "b7e4d2f819a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _agents_table() -> sa.TableClause:
    return sa.table(
        "agents",
        sa.column("id", sa.Integer()),
        sa.column("owner_id", sa.Integer()),
        sa.column("deployed_version_id", sa.Integer()),
    )


def _deployments_table() -> sa.TableClause:
    return sa.table(
        "deployments",
        sa.column("agent_id", sa.Integer()),
        sa.column("agent_version_id", sa.Integer()),
        sa.column("status", sa.String()),
        sa.column("access_mode", sa.String()),
        sa.column("api_key_hash", sa.String()),
        sa.column("rate_limit_per_user", sa.Integer()),
        sa.column("created_by", sa.Integer()),
    )


def upgrade() -> None:
    """Create the two tables, move every live pointer into them, drop the column."""
    op.create_table(
        "deployments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("agent_version_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("ACTIVE", "DISABLED", name="deploymentstatus"), nullable=False),
        sa.Column(
            "access_mode",
            sa.Enum("PRIVATE", "SELECTED_USERS", name="deploymentaccessmode"),
            nullable=False,
        ),
        sa.Column("api_key_hash", sa.String(), nullable=False),
        sa.Column("rate_limit_per_user", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"]),
        # No cascade on `created_by`: who published this is audit information —
        # the same call `approval_decisions.admin_id` makes.
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        # One Deployment per Agent (ADR-0029 §1) — enforced by the database, so
        # a second publish is a move rather than a second row whatever writes it.
        sa.UniqueConstraint("agent_id", name="uq_deployments_agent_id"),
    )
    op.create_table(
        "deployment_allowed_users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deployment_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deployment_id", "user_id", name="uq_deployment_allowed_users_pair"),
    )
    op.create_index(
        op.f("ix_deployment_allowed_users_deployment_id"), "deployment_allowed_users", ["deployment_id"]
    )
    op.create_index(op.f("ix_deployment_allowed_users_user_id"), "deployment_allowed_users", ["user_id"])

    _backfill_from_the_old_pointer()

    op.drop_constraint("fk_agents_deployed_version_id", "agents", type_="foreignkey")
    op.drop_column("agents", "deployed_version_id")


def _backfill_from_the_old_pointer() -> None:
    """One Deployment per agent that was serving something when this ran.

    Row by row rather than one `INSERT ... SELECT`, because each row needs a key
    of its own — a shared hash would make every deployment on the platform
    openable by one credential.
    """
    connection = op.get_bind()
    agents = _agents_table()
    deployed = connection.execute(
        sa.select(agents.c.id, agents.c.owner_id, agents.c.deployed_version_id).where(
            agents.c.deployed_version_id.is_not(None)
        )
    ).all()
    if not deployed:
        return
    connection.execute(
        _deployments_table().insert(),
        [
            {
                "agent_id": agent_id,
                "agent_version_id": version_id,
                "status": "ACTIVE",
                "access_mode": "PRIVATE",
                "api_key_hash": hash_api_key(generate_api_key()),
                "rate_limit_per_user": DEFAULT_RATE_LIMIT_PER_USER,
                "created_by": owner_id,
            }
            for agent_id, owner_id, version_id in deployed
        ],
    )


def downgrade() -> None:
    """Put the column back, pointed where the Deployment was pointing, and drop the tables."""
    op.add_column("agents", sa.Column("deployed_version_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_agents_deployed_version_id", "agents", "agent_versions", ["deployed_version_id"], ["id"]
    )

    # An agent with no Deployment gets `NULL`, which is what the column meant
    # before this migration existed — so no `WHERE` is needed to protect it.
    agents = _agents_table()
    deployments = _deployments_table()
    op.get_bind().execute(
        agents.update().values(
            deployed_version_id=sa.select(deployments.c.agent_version_id)
            .where(deployments.c.agent_id == agents.c.id)
            .scalar_subquery()
        )
    )

    op.drop_index(op.f("ix_deployment_allowed_users_user_id"), table_name="deployment_allowed_users")
    op.drop_index(op.f("ix_deployment_allowed_users_deployment_id"), table_name="deployment_allowed_users")
    op.drop_table("deployment_allowed_users")
    op.drop_table("deployments")
    # Postgres ENUM types outlive their columns — `drop_table` does not drop them.
    sa.Enum(name="deploymentaccessmode").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="deploymentstatus").drop(op.get_bind(), checkfirst=True)
