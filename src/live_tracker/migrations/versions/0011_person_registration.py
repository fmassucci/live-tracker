"""people: the registration number the company already uses for each person ("matrícula")

Optional and empty for everyone registered so far; two people can't share one.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-25 10:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0011'
down_revision: Union[str, Sequence[str], None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('people', schema=None) as batch_op:
        batch_op.add_column(sa.Column('registration', sa.String(length=40), server_default='', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('people', schema=None) as batch_op:
        batch_op.drop_column('registration')
