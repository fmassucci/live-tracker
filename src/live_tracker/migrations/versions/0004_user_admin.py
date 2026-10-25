"""user admin: administrators add accounts and manage the database (download and delete records)

Every account had the same access before, so the accounts that exist start as administrators.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-24 18:10:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_admin', sa.Boolean(), server_default=sa.text('0'), nullable=False))
    op.execute("UPDATE users SET is_admin = 1")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('is_admin')
