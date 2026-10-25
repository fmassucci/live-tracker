import sqlite3
from contextlib import closing

import pytest

from live_tracker.config import Config, load_config, rename_legacy_database


def test_missing_file_gives_the_defaults(tmp_path):
    assert load_config(tmp_path / "absent.toml") == Config()


def test_file_overrides_only_what_it_sets(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[camera]\nsource = "rtsp://cam/onvif1"\n[recognition]\naccept_threshold = 0.5\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.camera.source == "rtsp://cam/onvif1"
    assert cfg.recognition.accept_threshold == 0.5
    assert cfg.decision == Config().decision


def test_keys_from_older_versions_are_skipped(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[preview]\nenabled = true\nport = 9000\n", encoding="utf-8")
    assert load_config(path).preview.port == 9000


def test_typos_are_errors(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[camera]\nsorce = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="sorce"):
        load_config(path)


def _database(path, rows: int) -> None:
    """A database in WAL mode with `rows` rows, closed as the app leaves it."""
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE t (n INTEGER)")
        db.executemany("INSERT INTO t VALUES (?)", [(n,) for n in range(rows)])
        db.commit()


def test_a_database_under_the_old_name_is_used_until_start_renames_it(cfg):
    cfg.paths.data_dir.mkdir()
    new, legacy = cfg.paths.data_dir / "live-tracker.db", cfg.paths.data_dir / "portaria.db"
    assert cfg.db_path == new
    _database(legacy, rows=3)
    assert cfg.db_path == legacy
    assert rename_legacy_database(cfg)
    assert cfg.db_path == new and not legacy.exists()
    with closing(sqlite3.connect(new)) as db:
        assert db.execute("SELECT COUNT(*) FROM t").fetchone() == (3,)
    assert not rename_legacy_database(cfg)  # nothing left to rename


def test_a_database_still_open_keeps_its_old_name(cfg):
    cfg.paths.data_dir.mkdir()
    legacy = cfg.paths.data_dir / "portaria.db"
    _database(legacy, rows=1)
    with closing(sqlite3.connect(legacy)) as running:  # an older version of the app still uses it
        assert running.execute("SELECT COUNT(*) FROM t").fetchone() == (1,)
        assert not rename_legacy_database(cfg)
    assert cfg.db_path == legacy
