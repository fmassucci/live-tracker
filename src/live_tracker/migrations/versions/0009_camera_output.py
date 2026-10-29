"""camera output: the device output a door camera opens, such as a Tuya module's channel

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-27 16:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.add_column(sa.Column('output_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_cameras_output_id', 'device_outputs', ['output_id'], ['id'],
                                    ondelete='SET NULL')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.drop_constraint('fk_cameras_output_id', type_='foreignkey')
        batch_op.drop_column('output_id')
