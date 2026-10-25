"""record rule: when each camera keeps a photo of what its model finds

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-24 19:50:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.add_column(sa.Column('record_mode', sa.String(length=10), server_default='appear', nullable=False))
        batch_op.add_column(sa.Column('record_every_s', sa.Integer(), server_default='900', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.drop_column('record_every_s')
        batch_op.drop_column('record_mode')
