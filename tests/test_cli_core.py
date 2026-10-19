from cairn import config
from cairn.cli import main


def test_init_creates_config_and_data(env, capsys):
    assert main(["init", "--node", "laptop"]) == 0
    out = capsys.readouterr().out
    assert "Node initialized: laptop" in out
    cfg = config.load()
    assert cfg.node == "laptop"
    assert cfg.db_path.is_file()
    # second time: overwrites nothing
    assert main(["init", "--node", "otro"]) == 0
    assert "is already initialized" in capsys.readouterr().out
    assert config.load().node == "laptop"


def test_init_rejects_bad_node(env, capsys):
    assert main(["init", "--node", "Mi Laptop"]) == 1
    assert "invalid" in capsys.readouterr().err


def test_commands_need_init(env, capsys):
    assert main(["rebuild"]) == 1
    assert "cairn init" in capsys.readouterr().err


def test_rebuild(cfg, capsys):
    assert main(["rebuild"]) == 0
    assert "0 event(s)" in capsys.readouterr().out


def test_config_toml_escapes(env):
    config.write("laptop", author='Pablo "P" \\ Claude')
    assert config.load().author == 'Pablo "P" \\ Claude'
