"""object detection: each camera's model settings, and what a record saw

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-24 17:40:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.add_column(sa.Column('model_settings', sa.Text(), server_default='{}', nullable=False))
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('label', sa.String(length=40), server_default='', nullable=False))
        batch_op.add_column(sa.Column('count', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_column('count')
        batch_op.drop_column('label')
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.drop_column('model_settings')
