"""record while present: with an interval, whether a camera photographs again what stays in the picture

Until now, people and objects were photographed again every interval while they stayed, and faces only once
they came back. Each camera now chooses; they all start photographing again while it stays.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-24 23:20:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.add_column(sa.Column('record_while_present', sa.Boolean(), server_default=sa.text('1'),
                                      nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.drop_column('record_while_present')
