import stat

import pytest

from cairn import config, keys
from cairn.store import Store


def test_clave_del_nodo(tmp_path):
    path = tmp_path / "node_key"
    keys.generate(path, "laptop")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    line = path.with_suffix(".pub").read_text().strip()
    assert line.startswith("ssh-ed25519 ") and line.endswith(" cairn:laptop")
    again = keys.load_private(path)
    sig = keys.sign(again, "ab" * 32)
    pub = keys.parse_public(line)
    assert keys.verify(pub, "ab" * 32, sig)
    assert not keys.verify(pub, "cd" * 32, sig)
    assert not keys.verify(pub, "ab" * 32, None)
    assert not keys.verify(pub, "ab" * 32, "no-es-base64!")
    assert keys.normalize_public(line) == " ".join(line.split()[:2])
    with pytest.raises(FileExistsError):
        keys.generate(path, "laptop")                  # nunca pisa una clave


def test_clave_publica_invalida():
    for bad in ("", "ssh-rsa AAAAB3NzaC1yc2E", "ssh-ed25519 nobase64"):
        with pytest.raises(keys.KeyFileError):
            keys.parse_public(bad)


def test_store_genera_la_clave_y_firma(cfg):
    with Store(cfg) as s:
        assert cfg.key_path.exists()
        line = s.public_line()
        with s.writer() as w:
            ev = w.emit("create", "d1", {"name": "x", "meta": {}, "body": ""})
    assert keys.verify(keys.parse_public(line), ev.id, ev.sig)
    with Store(cfg) as s:
        assert s.public_line() == line                 # la segunda vez la lee


def test_config_remotos_y_pares(cfg):
    def mutate(raw):
        raw["remotes"] = {"vps": {"url": "ssh://cairn@host:2222", "key": "~/.ssh/k"}}
        raw["peers"] = {"vps": {"key": "ssh-ed25519 AAAA", "ssh": "false", "revoked": "2026-03-01"}}
        raw["sync"] = {"auto": "false"}
    c = config.update(mutate)
    assert c.remotes["vps"].url == "ssh://cairn@host:2222"
    assert c.remotes["vps"].key.name == "k" and c.remotes["vps"].key.is_absolute()
    assert c.peers["vps"] == config.Peer("vps", "ssh-ed25519 AAAA", False, "2026-03-01")
    assert c.sync_auto is False
    assert c.node == "laptop"
    assert "[remotes.vps]" in c.path.read_text()


def test_config_invalida(cfg):
    with pytest.raises(config.ConfigError, match="necesita `url`"):
        config.update(lambda raw: raw.update({"remotes": {"vps": {"key": "x"}}}))


def test_huella_como_ssh_keygen(tmp_path):
    import shutil
    import subprocess
    keys.generate(tmp_path / "node_key", "laptop")
    line = (tmp_path / "node_key.pub").read_text().strip()
    fp = keys.fingerprint(line)
    assert fp.startswith("SHA256:") and len(fp) == len("SHA256:") + 43
    assert keys.as_fingerprint(line) == keys.as_fingerprint(fp) == fp
    if shutil.which("ssh-keygen"):
        out = subprocess.run(["ssh-keygen", "-lf", str(tmp_path / "node_key.pub")],
                             capture_output=True, text=True, check=True).stdout
        assert out.split()[1] == fp
    for bad in ("SHA256:corta", "SHA256:" + "!" * 43, "MD5:aa:bb"):
        with pytest.raises(keys.KeyFileError):
            keys.as_fingerprint(bad)
