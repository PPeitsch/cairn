import io
import os
import sys
from pathlib import Path

import pytest

from cairn import docs
from cairn.cli import main
from cairn.markdown import parse_front_matter

HOY = "2026-03-10"


def run(capsys, *args, ok=True, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = main(list(args))
    out = capsys.readouterr()
    assert (code == 0) == ok, out
    return out


@pytest.fixture
def plan(store, env, monkeypatch, capsys):
    monkeypatch.chdir(env / "projects")
    run(capsys, "new", "api", "cache", "plan", "--repo", "api")
    return f"{HOY}_api_cache"


# ──────────────────────────────────────────────────────────────── edit

def test_edit_desde_stdin_cuerpo_y_front_matter(store, plan, capsys, monkeypatch):
    doc = docs.find(store, plan)
    text = docs.render(store, doc).replace("- [ ]", "- [x] hecho").replace(
        "estado: en_progreso", "estado: en_monitoreo\nnota: esperando el deploy")
    out = run(capsys, "edit", plan, "--stdin", stdin=text, monkeypatch=monkeypatch).out
    assert "Guardado" in out
    doc = docs.find(store, plan)
    assert "- [x] hecho" in doc.body
    assert (doc.estado, doc.meta["nota"]) == ("en_monitoreo", "esperando el deploy")
    assert "Sin cambios" in run(capsys, "edit", plan, "--stdin", stdin=text, monkeypatch=monkeypatch).out


def test_edit_sin_front_matter_solo_cambia_el_cuerpo(store, plan, capsys, monkeypatch):
    run(capsys, "edit", plan, "--stdin", stdin="# nuevo\n", monkeypatch=monkeypatch)
    doc = docs.find(store, plan)
    assert doc.body == "# nuevo\n" and doc.estado == "en_progreso"


def test_edit_set_unset_y_set_state(store, plan, capsys):
    run(capsys, "edit", plan, "--set", "nota=baja: prioridad")
    run(capsys, "edit", plan, "--set", "estado=propuesto")
    assert docs.find(store, plan).meta["nota"] == "baja: prioridad"
    run(capsys, "edit", plan, "--unset", "nota")
    assert "nota" not in docs.find(store, plan).meta
    ops = [r[0] for r in store.projection.db.execute(
        "SELECT op FROM revisions ORDER BY seq")]
    assert ops == ["create", "set_state", "set_state", "set_state"]


def test_edit_valida_estado_y_tipo(store, plan, capsys):
    assert "close / reopen" in run(capsys, "edit", plan, "--set", "estado=resuelto", ok=False).err
    assert "desconocido" in run(capsys, "edit", plan, "--set", "estado=raro", ok=False).err
    assert "tipo" in run(capsys, "edit", plan, "--set", "tipo=otro", ok=False).err
    assert "promote" in run(capsys, "edit", plan, "--set", "tipo=ref", ok=False).err
    assert "no se puede sacar" in run(capsys, "edit", plan, "--unset", "estado", ok=False).err
    assert "clave=valor" in run(capsys, "edit", plan, "--set", "x", ok=False).err
    run(capsys, "edit", plan, "--set", "tipo=bug")     # entre tipos con ciclo, sí


def test_edit_ref_marca_actualizado(store, env, capsys, monkeypatch):
    run(capsys, "new", "api", "endpoints", "ref")
    monkeypatch.setenv("CAIRN_NOW", "2026-04-01T10:00:00+00:00")
    run(capsys, "edit", "api_endpoints", "--set", "repo=api")
    assert docs.find(store, "api_endpoints").meta["actualizado"] == HOY   # sólo meta: no cambia
    run(capsys, "edit", "api_endpoints", "--stdin", stdin="# endpoints\n\nGET /x\n", monkeypatch=monkeypatch)
    assert docs.find(store, "api_endpoints").meta["actualizado"] == "2026-04-01"


def test_edit_ignora_cambios_en_los_vinculos(store, plan, capsys, monkeypatch):
    with store.writer() as w:
        w.emit("edit", docs.find(store, plan).id, {"body": docs.find(store, plan).body.replace(
            "<!-- La solución, con fases si el trabajo es incremental. -->", "LRU.")})
    run(capsys, "promote", plan, "api", "cache_ref")
    text = docs.render(store, docs.find(store, plan)).replace("../_ref/api_cache_ref.md", "../_ref/otra.md")
    err = run(capsys, "edit", plan, "--stdin", stdin=text, monkeypatch=monkeypatch).err
    assert "no se editan a mano" in err
    assert docs.find(store, plan).links == [["referencias", docs.find(store, "api_cache_ref").id]]


def test_edit_interactivo(store, plan, capsys, monkeypatch, tmp_path):
    editor = tmp_path / "editor.sh"
    editor.write_text('#!/bin/sh\nsed -i "s/^## Alcance$/## Alcance y límites/" "$1"\n')
    editor.chmod(0o755)
    monkeypatch.setenv("VISUAL", str(editor))
    assert "Guardado" in run(capsys, "edit", plan).out
    assert "## Alcance y límites" in docs.find(store, plan).body
    editor.write_text("#!/bin/sh\nexit 0\n")
    assert "Sin cambios" in run(capsys, "edit", plan).out
    editor.write_text("#!/bin/sh\nexit 3\n")
    assert "salió con 3" in run(capsys, "edit", plan, ok=False).err


def test_edit_interactivo_no_pisa_un_cambio_concurrente(store, plan, capsys, monkeypatch, tmp_path):
    editor = tmp_path / "editor.sh"
    # mientras el editor está abierto, otro proceso cambia el doc
    editor.write_text(f'#!/bin/sh\necho x >> "$1"\nCAIRN_NOW= {sys.executable} -m cairn.cli edit {plan} '
                      '--set nota=otro >/dev/null\n')
    editor.chmod(0o755)
    monkeypatch.setenv("VISUAL", str(editor))
    err = run(capsys, "edit", plan, ok=False).err
    assert "cambió mientras lo editabas" in err and "Tu versión quedó en" in err
    assert docs.find(store, plan).meta["nota"] == "otro"


# ──────────────────────────────────────────────────────────────── historial

def test_log_show_diff(store, plan, capsys, monkeypatch):
    run(capsys, "edit", plan, "--set", "nota=uno")
    run(capsys, "edit", plan, "--stdin", stdin="# reescrito\n", monkeypatch=monkeypatch)
    lines = run(capsys, "log", plan).out.splitlines()
    assert [ln.split()[4] for ln in lines] == ["edit", "set_state", "create"]
    primero = lines[-1].split()[0]
    assert "## TL;DR" in run(capsys, "show", f"{plan}@{primero}").out
    assert "nota: uno" not in run(capsys, "show", f"{plan}@{primero}").out
    assert run(capsys, "show", plan).out.endswith("# reescrito\n")
    assert "nota: uno" in run(capsys, "show", f"{plan}@~1").out
    d = run(capsys, "diff", plan).out
    assert "+# reescrito" in d and "-## TL;DR" in d
    d = run(capsys, "diff", plan, primero, "~1").out
    assert "+nota: uno" in d and "TL;DR" not in d
    assert "no hay ~5" in run(capsys, "show", f"{plan}@~5", ok=False).err
    assert "al menos 4" in run(capsys, "show", f"{plan}@ab", ok=False).err
    assert len(run(capsys, "log", "-n", "2").out.splitlines()) == 2


def test_diff_sin_cambios(store, plan, capsys):
    assert "Sin diferencias" in run(capsys, "diff", plan).out


# ──────────────────────────────────────────────────────────────── import / export

def write(root: Path, rel: str, text: str, mtime: float | None = None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


@pytest.fixture
def arbol(env):
    """Un árbol de wip-doc inventado: docs con vínculos, un handoff, basura y un link roto."""
    r = env / "arbol"
    write(r, "_done/2026-01-05_api_BUG_timeout.md",
          "---\ntipo: bug\nrepo: api\nestado: resuelto\nfecha: 2026-01-05\ncerrado: 2026-03-08\n"
          "autor: Pablo + Claude\nreferencias:\n  - ../_ref/api_limites.md\n---\n\n# api — timeout\n\n"
          "## TL;DR\n\nSe subió el timeout.\n", mtime=1_700_000_000)
    write(r, "_ref/api_limites.md",
          "---\ntipo: ref\nrepo: api\nfecha: 2026-01-05\nactualizado: 2026-01-06\n"
          "autor: Pablo + Claude\norigen:\n  - ../_done/2026-01-05_api_BUG_timeout.md\n"
          "  - ../_done/no_existe.md\n---\n\n# api — límites\n")
    write(r, "_wip/2026-03-01_api_cache.md",
          "---\ntipo: plan\nrepo: api\nestado: en_progreso\nnota: \"falta: medir\"\n"
          "fecha: 2026-03-01\nautor: Pablo + Claude\n---\n\n# api — cache\n\n- [ ] medir\n")
    write(r, "_wip/2026-03-09_api_HANDOFF_seguir.md",
          "---\ntipo: handoff\nrepo: api\nestado: pendiente\nfecha: 2026-03-09\nautor: Pablo + Claude\n"
          "plan:\n  - ../_wip/2026-03-01_api_cache.md\n---\n\n# api — handoff: seguir\n")
    write(r, "_wip/viejo_sin_front_matter.md", "# viejo\n\n**Estado:** abierto\n")
    write(r, "_wip/scripts/api_bench.py", "print(1)\n")
    write(r, "_wip/validations/api_resultado.md", "# no es un doc\n")
    write(r, "_wip/README.md", "# readme\n")
    return r


def test_import(store, arbol, capsys):
    out = run(capsys, "import", str(arbol), "--dry-run")
    assert "Se importarían: 4 doc(s), 3 vínculo(s)" in out.out
    assert store.projection.count_events() == 0
    out = run(capsys, "import", str(arbol))
    assert "Importados: 4 doc(s), 3 vínculo(s)" in out.out
    assert "viejo_sin_front_matter.md: sin front-matter" in out.err
    assert "api_limites: origen → ../_done/no_existe.md" in out.err
    assert "3 archivo(s) que no son docs" in out.err

    bug = docs.find(store, "2026-01-05_api_BUG_timeout")
    ref = docs.find(store, "api_limites")
    h = docs.find(store, "2026-03-09_api_HANDOFF_seguir")
    assert bug.scope == "done" and ref.scope == "ref" and h.scope == "wip"
    assert bug.links == [["referencias", ref.id]]
    assert ref.links == [["origen", bug.id]] and ref.meta["origen"] == ["../_done/no_existe.md"]
    assert h.links == [["plan", docs.find(store, "2026-03-01_api_cache").id]]
    assert bug.updated.startswith("2023-11-14")           # mtime del archivo
    ops = {r[0] for r in store.projection.db.execute("SELECT op FROM events")}
    assert ops == {"import"}

    # dos veces no duplica
    out = run(capsys, "import", str(arbol))
    assert "Importados: 0 doc(s)" in out.out and "ya está en la base" in out.err
    assert "[OK] todo en orden" in run(capsys, "check").out


def test_import_avisa_estado_que_no_corresponde_a_la_carpeta(store, env, capsys):
    r = env / "arbol"
    write(r, "_done/2026-01-01_a_b.md", "---\ntipo: plan\nestado: en_progreso\nfecha: 2026-01-01\n---\n# a\n")
    write(r, "_wip/2026-01-02_a_c.md", "---\nestado: en_progreso\nfecha: 2026-01-02\n---\n# c\n")
    err = run(capsys, "import", str(r)).err
    assert "no corresponde a _done/ — queda en wip" in err
    assert "tipo '' → infra" in err
    assert "no tiene _wip" in run(capsys, "import", str(env), ok=False).err


def test_export_reproduce_lo_importado(store, arbol, env, capsys):
    run(capsys, "import", str(arbol))
    dest = env / "export"
    out = run(capsys, "export", str(dest)).out
    assert "4 escrito(s)" in out
    for rel in ("_done/2026-01-05_api_BUG_timeout.md", "_wip/2026-03-01_api_cache.md",
                "_wip/2026-03-09_api_HANDOFF_seguir.md"):
        assert (dest / rel).read_text() == (arbol / rel).read_text(), rel
    meta, _ = parse_front_matter((dest / "_ref/api_limites.md").read_text())
    assert meta["origen"] == ["../_done/no_existe.md", "../_done/2026-01-05_api_BUG_timeout.md"]
    assert (dest / "_done/2026-01-05_api_BUG_timeout.md").stat().st_mtime == 1_700_000_000

    assert "0 escrito(s), 4 sin cambios" in run(capsys, "export", str(dest)).out
    run(capsys, "close", "2026-03-01_api_cache", "--force")
    err = run(capsys, "export", str(dest), ok=False).err
    assert "ya existen con otro contenido" in err      # el handoff apunta ahora a _done/
    r = run(capsys, "export", str(dest), "--force")
    assert "_wip/2026-03-01_api_cache.md no corresponde a ningún doc" in r.err
    assert (dest / "_done/2026-03-01_api_cache.md").is_file()
