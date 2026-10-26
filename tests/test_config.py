"""配置加载、数据目录与错误提示。"""

from __future__ import annotations

import os

import pytest

from hp4928 import config as config_mod


def test_env_overrides_port(monkeypatch):
    monkeypatch.setenv("HP4928_PORT", "18086")
    cfg = config_mod.load_config(extra={"paths": {"data_dir": ".tmp/cfg-env"}})
    assert cfg.port == 18086
    assert cfg.host == "0.0.0.0"


def test_data_dir_from_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("HP4928_DATA_DIR", str(tmp_path / "env-data"))
    cfg = config_mod.load_config(extra={"paths": {"data_dir": ".tmp/ignored"}})
    assert str(cfg.data_dir) == str(tmp_path / "env-data")
    assert cfg.db_path.parent == tmp_path / "env-data"


def test_ensure_layout_error_is_actionable(tmp_path, monkeypatch):
    cfg = config_mod.load_config(extra={"paths": {"data_dir": str(tmp_path / "ro")}})

    def boom(self, *args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(config_mod.Path, "mkdir", boom)
    with pytest.raises(config_mod.ConfigError) as excinfo:
        cfg.ensure_layout()
    message = str(excinfo.value)
    # 必须告诉运维到底改哪里
    assert "data_dir" in message
    assert "HP4928_DATA_DIR" in message
    assert "Read-only" in message


def test_ensure_layout_creates_subdirs(tmp_path):
    cfg = config_mod.load_config(extra={"paths": {"data_dir": str(tmp_path / "data")}})
    cfg.ensure_layout()
    for path in (cfg.data_dir, cfg.upload_dir, cfg.prepared_dir, cfg.spool_dir):
        assert path.is_dir()
    if os.name == "posix":
        assert oct(cfg.data_dir.stat().st_mode)[-3:] == "700"


def test_secret_key_is_stable(tmp_path):
    cfg = config_mod.load_config(extra={"paths": {"data_dir": str(tmp_path / "data")}})
    first = cfg.secret_key()
    second = config_mod.load_config(
        extra={"paths": {"data_dir": str(tmp_path / "data")}}
    ).secret_key()
    assert first and first == second
    assert len(first) > 30


def test_secret_key_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("HP4928_SECRET_KEY", "fixed-key-for-tests")
    cfg = config_mod.load_config(extra={"paths": {"data_dir": str(tmp_path / "d")}})
    assert cfg.secret_key() == "fixed-key-for-tests"


def test_dump_example_yaml_roundtrip():
    cfg = config_mod.load_config(extra={"paths": {"data_dir": ".tmp/cfg-dump"}})
    text = config_mod.dump_example_yaml(cfg)
    assert "port: 10086" in text
    assert "color: mono" in text
    assert "4x6in" in text
