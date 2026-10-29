"""agnostic devices: a camera switches the device output linked to it, with no fixed function such as a door

The camera's "door" role goes: its link is its output. A person's door access becomes access, an event's door
result becomes the action of the camera's device ("opened" is now "switched", "kept" is "denied"), and the door
screen's key is the recognition screen's.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-27 17:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.drop_column('device')
    with op.batch_alter_table('people', schema=None) as batch_op:
        batch_op.alter_column('door_access', new_column_name='access')
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.alter_column('door', new_column_name='action')
    op.execute("UPDATE events SET action = 'switched' WHERE action = 'opened'")
    op.execute("UPDATE events SET action = 'denied' WHERE action = 'kept'")
    op.execute("""UPDATE settings SET "key" = 'screen_key' WHERE "key" = 'door_screen_key'""")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("""UPDATE settings SET "key" = 'door_screen_key' WHERE "key" = 'screen_key'""")
    op.execute("UPDATE events SET action = 'opened' WHERE action = 'switched'")
    op.execute("UPDATE events SET action = 'kept' WHERE action = 'denied'")
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.alter_column('action', new_column_name='door')
    with op.batch_alter_table('people', schema=None) as batch_op:
        batch_op.alter_column('access', new_column_name='door_access')
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.add_column(sa.Column('device', sa.String(length=10), server_default='', nullable=False))
    op.execute("UPDATE cameras SET device = 'door' WHERE output_id IS NOT NULL")
