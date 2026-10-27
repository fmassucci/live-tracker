"""devices: ESP32 boards and Tuya modules, their outputs and inputs, and the commands sent to them

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-27 15:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('devices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('uid', sa.String(length=64), nullable=False),
    sa.Column('address', sa.String(length=64), nullable=False),
    sa.Column('local_key', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=80), nullable=False),
    sa.Column('new', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uid')
    )
    op.create_table('device_outputs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=20), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('pulse_ms', sa.Integer(), nullable=False),
    sa.Column('max_ms', sa.Integer(), nullable=False),
    sa.Column('timer_dp', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('device_id', 'key')
    )
    with op.batch_alter_table('device_outputs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_device_outputs_device_id'), ['device_id'], unique=False)

    op.create_table('device_inputs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=20), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('device_id', 'key')
    )
    with op.batch_alter_table('device_inputs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_device_inputs_device_id'), ['device_id'], unique=False)

    op.create_table('device_commands',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.Column('output_key', sa.String(length=20), nullable=False),
    sa.Column('action', sa.String(length=10), nullable=False),
    sa.Column('ms', sa.Integer(), nullable=False),
    sa.Column('requested_by', sa.String(length=60), nullable=False),
    sa.Column('requested_at', sa.DateTime(), nullable=False),
    sa.Column('done_at', sa.DateTime(), nullable=True),
    sa.Column('result', sa.String(length=10), nullable=False),
    sa.Column('error', sa.String(length=200), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('device_commands', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_device_commands_device_id'), ['device_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_device_commands_requested_at'), ['requested_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_device_commands_result'), ['result'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('device_commands')
    op.drop_table('device_inputs')
    op.drop_table('device_outputs')
    op.drop_table('devices')
