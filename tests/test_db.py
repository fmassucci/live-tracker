import sqlite3
import threading

from live_tracker.db import Database


def _lock_path(db_path):
    return db_path.parent / f"{db_path.name}.migrate.lock"


def test_two_programs_migrating_a_new_database_at_once_both_succeed(cfg):
    """The recognition service and the web UI are started together and each migrates. On a new database the
    second used to fail on a column the first had just added: "duplicate column name: analysis"."""
    together = threading.Barrier(2)
    failures: list[BaseException] = []

    def migrate() -> None:
        database = Database(cfg.db_path)  # before the barrier: only migrating is raced
        together.wait(timeout=30)
        try:
            database.migrate()
        except BaseException as error:  # noqa: BLE001 - the test reports whatever went wrong
            failures.append(error)

    threads = [threading.Thread(target=migrate, name=f"migrate-{i}") for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not [thread for thread in threads if thread.is_alive()], "a migration never finished"
    assert not failures, failures
    with sqlite3.connect(cfg.db_path) as connection:
        columns = [row[1] for row in connection.execute("pragma table_info(cameras)")]
        versions = [row[0] for row in connection.execute("select version_num from alembic_version")]
    assert "analysis" in columns  # the migration the second program used to fail on
    assert len(versions) == 1  # migrated once, not twice


def test_a_lock_file_left_behind_does_not_block_migrating(cfg):
    """The lock belongs to the operating system, which releases it when a killed process ends, so the file it
    was taken on is harmless on its own and is never deleted."""
    lock_path = _lock_path(cfg.db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(b"left by a process that was killed")

    Database(cfg.db_path).migrate()

    with sqlite3.connect(cfg.db_path) as connection:
        assert [row[0] for row in connection.execute("select version_num from alembic_version")]
    assert lock_path.exists()


def test_migrating_twice_in_a_row_changes_nothing(cfg):
    database = Database(cfg.db_path)
    database.migrate()
    with sqlite3.connect(cfg.db_path) as connection:
        before = [row[0] for row in connection.execute("select version_num from alembic_version")]

    database.migrate()

    with sqlite3.connect(cfg.db_path) as connection:
        assert [row[0] for row in connection.execute("select version_num from alembic_version")] == before
