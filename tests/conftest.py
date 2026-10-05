"""Cada test corre contra un nodo propio en un temporal: config, datos y proyectos aislados."""

import pytest

from cairn import config
from cairn.store import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CAIRN_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setenv("CAIRN_NOW", "2026-03-10T12:00:00+00:00")
    (tmp_path / "projects").mkdir()
    return tmp_path


@pytest.fixture
def cfg(env):
    config.write("laptop")
    return config.load()


@pytest.fixture
def store(cfg):
    s = Store(cfg)
    yield s
    s.close()
