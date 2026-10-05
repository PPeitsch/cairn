"""Lo que queda como archivos en la raíz de proyectos: restos de wip-doc y scripts/validations."""

from cairn import tree
from cairn.cli import main

HOY = "2026-03-10"


def run(capsys, *args, ok=True):
    code = main(list(args))
    out = capsys.readouterr()
    assert (code == 0) == ok, out
    return out


def test_raiz_vacia_o_inexistente_no_reporta(store, env):
    assert tree.problems(store) == []
    (env / "projects").rmdir()
    assert tree.problems(store) == []


def test_restos_de_wip_doc(store, env, capsys):
    root = env / "projects"
    run(capsys, "new", "api", "timeout", "--repo", "api")
    name = f"{HOY}_api_timeout"
    (root / "_wip" / "scripts").mkdir(parents=True)
    (root / "_wip" / "scripts" / "api_prueba.py").write_text("print(1)\n")
    (root / "_wip" / f"{name}.md").write_text("ya importado\n")
    (root / "_wip" / "2026-03-09_api_suelto.md").write_text("escrito a mano\n")
    (root / "_wip" / "README.md").write_text("no es un doc\n")
    (root / "_wip" / "datos.csv").write_text("a,b\n")
    (root / "_done" / "viejo").mkdir(parents=True)
    (root / "api" / "_wip").mkdir(parents=True)
    (root / "api" / "_wip" / "nota.md").write_text("de antes del árbol único\n")
    (root / "api" / "node_modules" / "x" / "_wip").mkdir(parents=True)
    (root / "api" / "node_modules" / "x" / "_wip" / "y.md").write_text("no se mira\n")

    p = tree.problems(store)
    assert any(f"_wip/{name}.md: el doc ya está en cairn" in x for x in p)
    assert any("_wip/2026-03-09_api_suelto.md: un doc fuera de cairn" in x for x in p)
    assert any("_wip/datos.csv no es un doc" in x for x in p)
    assert any("_done/viejo/ no es scripts/" in x for x in p)
    assert any(x.startswith("api/_wip/ tiene 1 .md adentro de un repo") for x in p)
    assert len(p) == 5, p          # ni el README, ni scripts/, ni node_modules

    out = run(capsys, "check", ok=False).out
    assert "un doc fuera de cairn" in out and "5 problema(s)" in out


def test_search_incluye_archivos_de_scripts_y_validations(store, env, capsys):
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
    assert "archivos de scripts/ y validations/  (1)" in out
    assert "_wip/validations/api_graficos/reporte.md" in out and "│ 3: El TIMEOUT bajó a 2 s." in out
