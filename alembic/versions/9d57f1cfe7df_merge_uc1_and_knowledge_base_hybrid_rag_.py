"""merge uc1 and knowledge-base-hybrid-rag branches

Revision ID: 9d57f1cfe7df
Revises: 3f9c7a2e6b18, c4a71e83f6d2
Create Date: 2026-08-18 11:12:24.604156

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d57f1cfe7df'
down_revision: Union[str, Sequence[str], None] = ('3f9c7a2e6b18', 'c4a71e83f6d2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
