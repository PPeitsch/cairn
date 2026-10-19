import io
import sys

import pytest

from cairn import docs
from cairn.cli import main

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

def test_edit_from_stdin_body_and_front_matter(store, plan, capsys, monkeypatch):
    doc = docs.find(store, plan)
    text = docs.render(store, doc).replace("- [ ]", "- [x] hecho").replace(
        "estado: en_progreso", "estado: en_monitoreo\nnota: esperando el deploy")
    out = run(capsys, "edit", plan, "--stdin", stdin=text, monkeypatch=monkeypatch).out
    assert "Guardado" in out
    doc = docs.find(store, plan)
    assert "- [x] hecho" in doc.body
    assert (doc.estado, doc.meta["nota"]) == ("en_monitoreo", "esperando el deploy")
    assert "Sin cambios" in run(capsys, "edit", plan, "--stdin", stdin=text, monkeypatch=monkeypatch).out


def test_edit_without_front_matter_changes_only_the_body(store, plan, capsys, monkeypatch):
    run(capsys, "edit", plan, "--stdin", stdin="# nuevo\n", monkeypatch=monkeypatch)
    doc = docs.find(store, plan)
    assert doc.body == "# nuevo\n" and doc.estado == "en_progreso"


def test_edit_set_unset_and_set_state(store, plan, capsys):
    run(capsys, "edit", plan, "--set", "nota=baja: prioridad")
    run(capsys, "edit", plan, "--set", "estado=propuesto")
    assert docs.find(store, plan).meta["nota"] == "baja: prioridad"
    run(capsys, "edit", plan, "--unset", "nota")
    assert "nota" not in docs.find(store, plan).meta
    ops = [r[0] for r in store.projection.db.execute(
        "SELECT op FROM revisions ORDER BY seq")]
    assert ops == ["create", "set_state", "set_state", "set_state"]


def test_edit_validates_status_and_type(store, plan, capsys):
    assert "close / reopen" in run(capsys, "edit", plan, "--set", "estado=resuelto", ok=False).err
    assert "desconocido" in run(capsys, "edit", plan, "--set", "estado=raro", ok=False).err
    assert "tipo" in run(capsys, "edit", plan, "--set", "tipo=otro", ok=False).err
    assert "promote" in run(capsys, "edit", plan, "--set", "tipo=ref", ok=False).err
    assert "no se puede sacar" in run(capsys, "edit", plan, "--unset", "estado", ok=False).err
    assert "clave=valor" in run(capsys, "edit", plan, "--set", "x", ok=False).err
    run(capsys, "edit", plan, "--set", "tipo=bug")     # between types with a lifecycle, yes


def test_edit_ref_marks_updated(store, env, capsys, monkeypatch):
    run(capsys, "new", "api", "endpoints", "ref")
    monkeypatch.setenv("CAIRN_NOW", "2026-04-01T10:00:00+00:00")
    run(capsys, "edit", "api_endpoints", "--set", "repo=api")
    assert docs.find(store, "api_endpoints").meta["actualizado"] == HOY   # meta only: it does not change
    run(capsys, "edit", "api_endpoints", "--stdin", stdin="# endpoints\n\nGET /x\n", monkeypatch=monkeypatch)
    assert docs.find(store, "api_endpoints").meta["actualizado"] == "2026-04-01"


def test_edit_ignores_link_changes(store, plan, capsys, monkeypatch):
    with store.writer() as w:
        w.emit("edit", docs.find(store, plan).id, {"body": docs.find(store, plan).body.replace(
            "<!-- La solución, con fases si el trabajo es incremental. -->", "LRU.")})
    run(capsys, "promote", plan, "api", "cache_ref")
    text = docs.render(store, docs.find(store, plan)).replace("../_ref/api_cache_ref.md", "../_ref/otra.md")
    err = run(capsys, "edit", plan, "--stdin", stdin=text, monkeypatch=monkeypatch).err
    assert "no se editan a mano" in err
    assert docs.find(store, plan).links == [["referencias", docs.find(store, "api_cache_ref").id]]


def test_interactive_edit(store, plan, capsys, monkeypatch, tmp_path):
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


def test_interactive_edit_does_not_overwrite_a_concurrent_change(store, plan, capsys, monkeypatch, tmp_path):
    editor = tmp_path / "editor.sh"
    # while the editor is open, another process changes the doc
    editor.write_text(f'#!/bin/sh\necho x >> "$1"\nCAIRN_NOW= {sys.executable} -m cairn.cli edit {plan} '
                      '--set nota=otro >/dev/null\n')
    editor.chmod(0o755)
    monkeypatch.setenv("VISUAL", str(editor))
    err = run(capsys, "edit", plan, ok=False).err
    assert "cambió mientras lo editabas" in err and "Tu versión quedó en" in err
    assert docs.find(store, plan).meta["nota"] == "otro"


# ──────────────────────────────────────────────────────────────── history

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


def test_diff_without_changes(store, plan, capsys):
    assert "Sin diferencias" in run(capsys, "diff", plan).out
