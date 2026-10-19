"""Files that are not docs, in the `[docs] file_dirs` subfolders of the projects root."""

import pytest

from cairn import config, tree
from cairn.cli import main
from cairn.store import Store

HOY = "2026-03-10"


def run(capsys, *args, ok=True):
    code = main(list(args))
    out = capsys.readouterr()
    assert (code == 0) == ok, out
    return out


@pytest.fixture
def store(cfg):
    """A node with `[docs] file_dirs = "scripts, validations"`."""
    config.update(lambda raw: raw.setdefault("docs", {}).update(file_dirs="scripts, validations"))
    s = Store(config.load())
    yield s
    s.close()


def test_empty_or_missing_root_reports_nothing(store, env):
    assert tree.problems(store) == []
    (env / "projects").rmdir()
    assert tree.problems(store) == []


def test_files_out_of_place(store, env, capsys):
    root = env / "projects"
    (root / "_wip" / "scripts").mkdir(parents=True)
    (root / "_wip" / "scripts" / "api_prueba.py").write_text("print(1)\n")
    (root / "_wip" / "2026-03-09_api_suelto.md").write_text("escrito a mano\n")
    (root / "_wip" / "README.md").write_text("no es un doc\n")
    (root / "_wip" / "datos.csv").write_text("a,b\n")
    (root / "_done" / "viejo").mkdir(parents=True)

    p = tree.problems(store)
    assert any("_wip/2026-03-09_api_suelto.md: a stray .md" in x for x in p)
    assert any("_wip/datos.csv is not a doc — it goes in _wip/ + scripts/ or validations/" in x for x in p)
    assert any("_done/viejo/ is not a files folder (scripts/ or validations/)" in x for x in p)
    assert len(p) == 3, p          # not the README nor scripts/

    out = run(capsys, "check", ok=False).out
    assert "a stray .md" in out and "3 problem(s)" in out


def test_search_includes_scripts_and_validations_files(store, env, capsys):
    root = env / "projects"
    run(capsys, "new", "api", "timeout", "--repo", "api")
    v = root / "_wip" / "validations" / "api_graficos"
    v.mkdir(parents=True)
    (v / "reporte.md").write_text("# Reporte\n\nEl TIMEOUT bajó a 2 s.\n")
    (root / "_wip" / "scripts").mkdir()
    (root / "_wip" / "scripts" / "otro.md").write_text("nada que ver\n")

    hits = tree.search_files(store, "timeout")
    assert [(str(h.path), h.lines) for h in hits] == [
        ("_wip/validations/api_graficos/reporte.md", [(3, "El TIMEOUT bajó a 2 s.")])]
    assert tree.search_files(store, "timeout", ("ref",)) == []

    out = run(capsys, "search", "bajó a 2").out
    assert "files in scripts/ and validations/  (1)" in out
    assert "_wip/validations/api_graficos/reporte.md" in out and "│ 3: El TIMEOUT bajó a 2 s." in out
