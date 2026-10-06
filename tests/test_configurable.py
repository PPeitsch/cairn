"""Lo que es preferencia del usuario sale de la config, no del código."""

import json

import pytest

from cairn import config, docs, integrate, mcp_server, templates, tree
from cairn.cli import main
from cairn.store import Store

HOY = "2026-03-10"


def run(capsys, *args, ok=True):
    code = main(list(args))
    out = capsys.readouterr()
    assert (code == 0) == ok, out
    return out


def set_cfg(**tables):
    def mutate(raw):
        for t, values in tables.items():
            raw.setdefault(t, {}).update(values)
    return config.update(mutate)


def test_defaults(cfg):
    assert (cfg.sync_timeout, cfg.sync_hook_timeout, cfg.handoff_stale_days) == (60, 15, 14)
    assert cfg.file_dirs == ("scripts", "validations") and cfg.max_depth == 4
    assert (cfg.mcp_name, cfg.start_timeout, cfg.end_timeout) == ("cairn", 30, 10)
    assert cfg.templates_dir is None and cfg.mcp_instructions is None


def test_autor_por_defecto_sin_hardcodeo(env, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text("[user]\n\tname = Ada Lovelace\n")
    assert config.default_author() == "Ada Lovelace"
    config.write("laptop")
    assert config.load().author == "Ada Lovelace"
    (tmp_path / "gitconfig").write_text("")
    import getpass
    assert config.default_author() == getpass.getuser()


def test_valores_invalidos(cfg):
    with pytest.raises(config.ConfigError, match="número"):
        set_cfg(sync={"timeout": "rápido"})
    with pytest.raises(config.ConfigError, match="mayor que 0"):
        set_cfg(docs={"handoff_stale_days": "0"})
    with pytest.raises(config.ConfigError, match="mcp_name"):
        set_cfg(claude_code={"mcp_name": "con espacio"})


def test_plantillas_propias(cfg, env, capsys):
    d = env / "plantillas"
    d.mkdir()
    (d / "bug.md").write_text("# {titulo}\n\n## Qué pasó\n")
    (d / "handoff.md").write_text("\n# {titulo}\n\n## Para la próxima\n")
    set_cfg(docs={"templates": str(d)})
    with Store(config.load()) as s:
        b = docs.new(s, "api", "lento", "bug", "api").doc
        assert b.body == "\n# api — lento\n\n## Qué pasó\n"
        p = docs.new(s, "api", "cache", "plan", "api").doc     # sin archivo: la de fábrica
        assert "## Diseño / propuesta" in p.body
        h = docs.handoff_new(s, "api", "sigue", repo="api").doc
        assert "## Para la próxima" in h.body


def test_defaults_exporta_y_se_puede_usar(cfg, env, capsys):
    out = run(capsys, "defaults", str(env / "mio")).out
    assert "templates = " in out and "instructions = " in out
    assert (env / "mio" / "templates" / "plan.md").read_text() == templates.PLAN.lstrip("\n")
    assert (env / "mio" / "mcp_instructions.md").read_text() == mcp_server.INSTRUCTIONS
    assert "ya existe" in run(capsys, "defaults", str(env / "mio")).err
    set_cfg(docs={"templates": str(env / "mio" / "templates")})
    with Store(config.load()) as s:
        assert docs.new(s, "api", "x", "plan", "api").doc.body == \
            templates.body(None, "plan", "api — x")


def test_instrucciones_del_mcp(cfg, env):
    assert mcp_server.instructions() == mcp_server.INSTRUCTIONS
    (env / "inst.md").write_text("Mis convenciones.\n")
    set_cfg(mcp={"instructions": str(env / "inst.md")})
    assert mcp_server.instructions() == "Mis convenciones.\n"


def test_handoff_viejo_configurable(cfg, capsys):
    with Store(config.load()) as s:
        docs.handoff_new(s, "api", "sigue", repo="api")
    import os
    os.environ["CAIRN_NOW"] = "2026-03-15T12:00:00+00:00"
    try:
        assert "VIEJA" not in run(capsys, "handoff", "list", "--all").out
        set_cfg(docs={"handoff_stale_days": "3"})
        assert "VIEJA" in run(capsys, "handoff", "list", "--all").out
    finally:
        os.environ["CAIRN_NOW"] = "2026-03-10T12:00:00+00:00"


def test_carpetas_de_archivos_configurables(cfg, env):
    root = env / "projects"
    (root / "_wip" / "notas").mkdir(parents=True)
    (root / "_wip" / "notas" / "x.md").write_text("una aguja\n")
    with Store(config.load()) as s:
        assert any("_wip/notas/ no es una carpeta de archivos" in p for p in tree.problems(s))
        assert tree.search_files(s, "aguja") == []
    set_cfg(docs={"file_dirs": "scripts, notas"})
    with Store(config.load()) as s:
        assert tree.problems(s) == []
        assert [str(h.path) for h in tree.search_files(s, "aguja")] == ["_wip/notas/x.md"]


def test_integrate_toma_nombre_y_timeouts(cfg, env, monkeypatch):
    d = env / "claude"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    set_cfg(claude_code={"mcp_name": "docs", "start_timeout": "45", "end_timeout": "5"})
    rep = integrate.claude_code(["/opt/cairn"], mcp=False)
    hooks = json.loads((d / "settings.json").read_text())["hooks"]
    assert hooks["SessionStart"][0]["hooks"][0]["timeout"] == 45
    assert hooks["SessionEnd"][0]["hooks"][0]["timeout"] == 5
    assert len(rep.actions) == 2
    # cambiar sólo el timeout también es un cambio
    set_cfg(claude_code={"start_timeout": "60"})
    rep = integrate.claude_code(["/opt/cairn"], mcp=False)
    start = json.loads((d / "settings.json").read_text())["hooks"]["SessionStart"]
    assert start[0]["hooks"][0]["timeout"] == 60 and len(start) == 1
    assert integrate.Settings.from_config().mcp_name == "docs"
