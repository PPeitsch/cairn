"""Each test runs against its own node in a temp dir: isolated config, data and projects."""

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
    config.write("laptop", author="Pablo + Claude")
    return config.load()


@pytest.fixture
def store(cfg):
    s = Store(cfg)
    yield s
    s.close()


class Peer:
    """Another node, with its key, authorized in the test node's config."""

    def __init__(self, name, tmp_path):
        from cairn import keys
        self.name = name
        self._keys = keys
        self.key = keys.generate(tmp_path / f"peer-{name}" / "node_key", name)
        line = keys.public_line(self.key.public_key(), name)
        config.update(lambda raw: raw.setdefault("peers", {}).update({name: {"key": line}}))

    def sign(self, ev):
        from cairn.events import Event
        return Event(**{**ev.__dict__, "sig": self._keys.sign(self.key, ev.id)})


@pytest.fixture
def peer(cfg, tmp_path):
    """`peer("vps")`: authorizes a node and returns what to sign its events with. Open the store
    afterwards (`Store(config.load())`) so it knows about it."""
    return lambda name: Peer(name, tmp_path)
