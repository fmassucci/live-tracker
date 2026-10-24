"""camera analysis and device: what the camera analyzes, and what it drives

The role column mixed the two: "door" meant both "recognizes people" and "opens the door".
Every camera now runs its analysis (face recognition); the door is a device attached to a camera.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24 15:10:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.add_column(sa.Column('analysis', sa.String(length=20), server_default='faces', nullable=False))
        batch_op.add_column(sa.Column('device', sa.String(length=10), server_default='', nullable=False))
    op.execute("UPDATE cameras SET device = 'door' WHERE role = 'door'")
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.drop_column('role')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.add_column(sa.Column('role', sa.String(length=10), server_default='view', nullable=False))
    op.execute("UPDATE cameras SET role = 'door' WHERE device = 'door'")
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.drop_column('device')
        batch_op.drop_column('analysis')
