"""Alembic environment. Migrations run from Database.migrate(), which passes the database URL."""

from alembic import context
from sqlalchemy import engine_from_config, pool

from live_tracker.db import Base

config = context.config
engine = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)

with engine.connect() as connection:
    # Batch mode rebuilds tables for changes SQLite's ALTER TABLE can't do.
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
