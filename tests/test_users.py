from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import select, text

import pytest

from live_tracker.db import MIGRATIONS_DIR, Database, User
from live_tracker.users import (
    AccountError, create_user, ensure_default_user, is_admin, remove_user, rename_user, set_admin,
)


def test_the_first_account_is_an_administrator_and_new_ones_are_not_unless_asked(db):
    with db.session() as session:
        ensure_default_user(session)
        create_user(session, "caixa", "senha-do-caixa")
        create_user(session, "gerente", "senha-do-gerente", admin=True)
    with db.session() as session:
        assert is_admin(session, "admin") and is_admin(session, "gerente")
        assert not is_admin(session, "caixa") and not is_admin(session, "nobody")


def test_the_last_administrator_cannot_be_removed_or_made_a_user(db):
    with db.session() as session:
        ensure_default_user(session)
        manager = create_user(session, "gerente", "senha-do-gerente", admin=True)
        session.flush()
        set_admin(session, manager, False)  # admin is still there
        with pytest.raises(AccountError):
            set_admin(session, session.scalars(select(User).where(User.username == "admin")).one(), False)
        with pytest.raises(AccountError):
            remove_user(session, session.scalars(select(User).where(User.username == "admin")).one())
        remove_user(session, manager)


def test_a_name_another_account_has_is_taken_in_upper_or_lower_case(db):
    with db.session() as session:
        ensure_default_user(session)
        cashier = create_user(session, "caixa", "senha-do-caixa")
        session.flush()
        with pytest.raises(AccountError):
            create_user(session, "Caixa", "outra-senha-1")
        with pytest.raises(AccountError):
            rename_user(session, cashier, "ADMIN")
        with pytest.raises(AccountError):
            rename_user(session, cashier, "a b")
        rename_user(session, cashier, " Caixa ")  # its own name in other letters: still its own
        assert cashier.username == "Caixa"


def test_accounts_from_before_administrators_existed_keep_their_access(tmp_path):
    """Everyone had the same access then, so they all become administrators."""
    db = Database(tmp_path / "live-tracker.db")
    alembic_cfg = AlembicConfig()
    alembic_cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    alembic_cfg.set_main_option("sqlalchemy.url", db.url)
    command.upgrade(alembic_cfg, "0003")
    with db.engine.begin() as connection:
        connection.execute(text("INSERT INTO users (username, password_hash, created_at) VALUES ('ana', 'x', '2026-10-01')"))
    db.migrate()
    with db.session() as session:
        assert is_admin(session, "ana")
