"""`integrate claude-code` contra una config de Claude Code aislada y un `claude` falso."""

import json
import stat
import sys
import textwrap

import pytest

from cairn import cli, integrate

WIP_DOC_HOOK = {"matcher": "startup|clear", "hooks": [
    {"type": "command", "command": "/x/skills/wip-doc/driver.py handoff hook", "timeout": 10}]}

FAKE_CLAUDE = textwrap.dedent("""\
    # emula `claude mcp add|remove --scope user` sobre $CLAUDE_CONFIG_DIR/.claude.json
    import json, os, sys
    from pathlib import Path
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    a = sys.argv[1:]
    with open(Path(os.environ["CLAUDE_CONFIG_DIR"]) / "calls.log", "a") as f:
        f.write(" ".join(a) + "\\n")
    servers = data.setdefault("mcpServers", {})
    if a[:4] == ["mcp", "add", "--scope", "user"]:
        name, cmd = a[4], a[6:]
        servers[name] = {"type": "stdio", "command": cmd[0], "args": cmd[1:], "env": {}}
    elif a[:4] == ["mcp", "remove", "--scope", "user"]:
        if a[4] not in servers:
            sys.exit(1)
        del servers[a[4]]
    else:
        sys.exit(2)
    path.write_text(json.dumps(data))
""")


@pytest.fixture
def cc(tmp_path, monkeypatch):
    d = tmp_path / "claude"
    d.mkdir()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    fake = bin_ / "claude"
    fake.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    monkeypatch.setenv("PATH", str(bin_))
    (d / "settings.json").write_text(json.dumps(
        {"model": "x", "hooks": {"SessionStart": [WIP_DOC_HOOK]}}, indent=2))
    return d


def settings(cc) -> dict:
    return json.loads((cc / "settings.json").read_text())


def calls(cc) -> list[str]:
    p = cc / "calls.log"
    return p.read_text().splitlines() if p.exists() else []


CMD = ["/opt/cairn/bin/cairn"]


def test_instala_sin_pisar_lo_ajeno(cc):
    rep = integrate.claude_code(CMD)
    assert len(rep.actions) == 2
    s = settings(cc)
    assert s["model"] == "x"
    start = s["hooks"]["SessionStart"]
    assert start[0] == WIP_DOC_HOOK                        # wip-doc sigue ahí, primero
    assert start[1] == {"matcher": "startup|clear", "hooks": [
        {"type": "command", "command": "/opt/cairn/bin/cairn handoff hook", "timeout": 10}]}
    assert integrate.registered_mcp()["args"] == ["mcp"]
    assert calls(cc) == ["mcp add --scope user cairn -- /opt/cairn/bin/cairn mcp"]


def test_idempotente(cc):
    integrate.claude_code(CMD)
    before = (cc / "settings.json").read_text()
    rep = integrate.claude_code(CMD)
    assert rep.actions == [] and len(rep.unchanged) == 2
    assert (cc / "settings.json").read_text() == before
    assert len(calls(cc)) == 1


def test_cambio_de_comando_reemplaza(cc):
    integrate.claude_code(CMD)
    rep = integrate.claude_code(["ssh", "vps", "cairn"])
    assert "sacar hook SessionStart: /opt/cairn/bin/cairn handoff hook" in rep.actions
    hooks = [h["command"] for g in settings(cc)["hooks"]["SessionStart"] for h in g["hooks"]]
    assert hooks == [WIP_DOC_HOOK["hooks"][0]["command"], "ssh vps cairn handoff hook"]
    assert integrate.registered_mcp() == {"type": "stdio", "command": "ssh",
                                          "args": ["vps", "cairn", "mcp"], "env": {}}
    assert calls(cc)[1:] == ["mcp remove --scope user cairn",
                             "mcp add --scope user cairn -- ssh vps cairn mcp"]


def test_remove_deja_lo_ajeno(cc):
    original = settings(cc)
    integrate.claude_code(CMD)
    rep = integrate.claude_code(CMD, remove=True)
    assert len(rep.actions) == 2
    assert settings(cc) == original
    assert integrate.registered_mcp() is None
    assert integrate.claude_code(CMD, remove=True).actions == []


def test_remove_borra_grupos_vacios(cc):
    (cc / "settings.json").write_text("{}")
    integrate.claude_code(CMD)
    integrate.claude_code(CMD, remove=True)
    assert settings(cc) == {}


def test_dry_run_no_toca_nada(cc):
    before = (cc / "settings.json").read_text()
    rep = integrate.claude_code(CMD, dry_run=True)
    assert len(rep.actions) == 2
    assert (cc / "settings.json").read_text() == before
    assert calls(cc) == []


def test_sin_settings(cc):
    (cc / "settings.json").unlink()
    integrate.claude_code(CMD, mcp=False)
    hook = settings(cc)["hooks"]["SessionStart"][0]["hooks"][0]
    assert hook["command"] == "/opt/cairn/bin/cairn handoff hook"


def test_settings_invalido_no_se_toca(cc):
    (cc / "settings.json").write_text("{roto")
    with pytest.raises(integrate.CairnError, match="no es JSON válido"):
        integrate.claude_code(CMD)
    assert (cc / "settings.json").read_text() == "{roto"
    assert calls(cc) == []


def test_sin_claude(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "vacio"))
    with pytest.raises(integrate.CairnError, match="claude"):
        integrate.claude_code(CMD, hooks=False)


def test_reconoce_hooks_de_cairn():
    spec = integrate.HOOKS[0]
    assert integrate._is_cairn_hook("/a/b/cairn handoff hook", spec)
    assert integrate._is_cairn_hook("/usr/bin/python3 -m cairn handoff hook", spec)
    assert integrate._is_cairn_hook("ssh vps cairn handoff hook", spec)
    assert not integrate._is_cairn_hook("/x/wip-doc/driver.py handoff hook", spec)
    assert not integrate._is_cairn_hook("/x/cairn-tools/otro handoff hook", spec)
    assert not integrate._is_cairn_hook("cairn handoff list", spec)


def test_cli(cc, capsys):
    assert cli.main(["integrate", "claude-code", "--command", "/opt/cairn/bin/cairn", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[dry-run] agregar hook SessionStart" in out and "[dry-run] agregar MCP cairn" in out
    assert cli.main(["integrate", "claude-code", "--command", "/opt/cairn/bin/cairn"]) == 0
    assert cli.main(["integrate", "claude-code", "--command", "/opt/cairn/bin/cairn"]) == 0
    assert "ya estaba" in capsys.readouterr().out
