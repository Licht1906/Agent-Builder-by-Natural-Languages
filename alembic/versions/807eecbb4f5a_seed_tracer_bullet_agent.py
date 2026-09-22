"""seed tracer bullet agent

Revision ID: 807eecbb4f5a
Revises: 1135e617defc
Create Date: 2026-07-31 14:50:10.637655

The runtime tracer bullet (ticket #5, ADR-0003) needs an `AgentSpec` already
sitting in the database so `POST /agents/{id}/chat` has something to run the
moment migrations finish. `status=APPROVED` and `deployed_version_id` are set
directly here because the version-lifecycle write path and its gates (#7,
#14, #15) don't exist yet — a migration writing rows by hand, not application
code skipping a business rule that exists.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from src.agents.seed import TRACER_BULLET_AGENT_NAME, tracer_bullet_spec_payload
from src.config import get_settings

# revision identifiers, used by Alembic.
revision: str = '807eecbb4f5a'
down_revision: str | Sequence[str] | None = '1135e617defc'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _users_table() -> sa.TableClause:
    return sa.table('users', sa.column('id', sa.Integer()), sa.column('email', sa.String()))


def _agents_table() -> sa.TableClause:
    return sa.table(
        'agents',
        sa.column('id', sa.Integer()),
        sa.column('owner_id', sa.Integer()),
        sa.column('name', sa.String()),
        sa.column('deployed_version_id', sa.Integer()),
    )


def _agent_versions_table() -> sa.TableClause:
    return sa.table(
        'agent_versions',
        sa.column('id', sa.Integer()),
        sa.column('agent_id', sa.Integer()),
        sa.column('spec', sa.JSON()),
        sa.column('status', sa.String()),
    )


def upgrade() -> None:
    """Insert agent, version, then point `deployed_version_id` at it — three steps
    because the version needs the agent's id and the pointer needs the version's.
    """
    connection = op.get_bind()
    creator_email = get_settings().seed_creator_email
    users = _users_table()
    creator_id = connection.execute(sa.select(users.c.id).where(users.c.email == creator_email)).scalar_one()

    agents = _agents_table()
    agent_id = connection.execute(
        agents.insert().values(owner_id=creator_id, name=TRACER_BULLET_AGENT_NAME).returning(agents.c.id)
    ).scalar_one()

    versions = _agent_versions_table()
    version_id = connection.execute(
        versions.insert()
        .values(agent_id=agent_id, spec=tracer_bullet_spec_payload(), status='APPROVED')
        .returning(versions.c.id)
    ).scalar_one()

    connection.execute(agents.update().where(agents.c.id == agent_id).values(deployed_version_id=version_id))


def downgrade() -> None:
    """Remove the seeded agent and its version, leaving anything created since."""
    connection = op.get_bind()
    agents = _agents_table()
    versions = _agent_versions_table()
    agent_id = connection.execute(
        sa.select(agents.c.id).where(agents.c.name == TRACER_BULLET_AGENT_NAME)
    ).scalar_one_or_none()
    if agent_id is None:
        return
    connection.execute(agents.update().where(agents.c.id == agent_id).values(deployed_version_id=None))
    connection.execute(versions.delete().where(versions.c.agent_id == agent_id))
    connection.execute(agents.delete().where(agents.c.id == agent_id))
