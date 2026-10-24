import pytest

from portaria.config import Config, load_config


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
