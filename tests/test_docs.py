"""Paridad con wip-doc: los mismos casos que sus tests, contra la base de eventos."""

import json
import subprocess

import pytest

from cairn import docs
from cairn.cli import main
from cairn.markdown import parse_front_matter
from cairn.store import CairnError

HOY = "2026-03-10"


def run(capsys, *args, ok=True):
    code = main(list(args))
    out = capsys.readouterr()
    assert (code == 0) == ok, out
    return out


def fill(store, ref, old, new):
    """Reemplaza texto en el cuerpo (hasta que esté `edit`, con un evento a mano)."""
    doc = docs.find(store, ref)
    assert old in doc.body, old
    with store.writer() as w:
        w.emit("edit", doc.id, {"body": doc.body.replace(old, new, 1)})


@pytest.fixture
def repo(env, monkeypatch):
    """Un repo git bajo la raíz de proyectos, como PWD."""
    r = env / "projects" / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    monkeypatch.chdir(r)
    return r


# ──────────────────────────────────────────────────────────────── new

def test_new_estampa_el_repo_del_pwd(store, repo, capsys):
    out = run(capsys, "new", "bcra", "rate_limit", "bug").out
    assert out.strip() == f"{HOY}_bcra_BUG_rate_limit"
    doc = docs.find(store, f"{HOY}_bcra_BUG_rate_limit.md")
    assert doc.meta == {"tipo": "bug", "repo": "repo", "estado": "en_progreso", "fecha": HOY,
                        "autor": "Pablo + Claude"}
    assert doc.scope == "wip" and doc.title == "bcra — rate limit"
    assert "## Discoveries (en orden)" in doc.body


def test_new_ref_sin_fecha_y_plan(store, repo, capsys):
    run(capsys, "new", "bcra", "endpoints", "ref")
    ref = docs.find(store, "bcra_endpoints")
    assert ref.scope == "ref" and ref.meta["actualizado"] == HOY and "estado" not in ref.meta
    assert "## Contenido" in ref.body
    run(capsys, "new", "bcra", "cache", "plan")
    assert "## Diseño / propuesta" in docs.find(store, f"{HOY}_bcra_cache").body


def test_new_cross_repo_y_repo_explicito(store, env, monkeypatch, capsys):
    monkeypatch.chdir(env / "projects")
    run(capsys, "new", "infra", "vpn")
    assert "repo" not in docs.find(store, f"{HOY}_infra_vpn").meta
    run(capsys, "new", "infra", "dns", "--repo", "varios")
    assert docs.find(store, f"{HOY}_infra_dns").meta["repo"] == "varios"


def test_new_no_pisa_y_valida(store, repo, capsys):
    run(capsys, "new", "a", "b")
    assert "Ya existe" in run(capsys, "new", "a", "b", ok=False).err
    assert "Tipo inválido" in run(capsys, "new", "a", "c", "otro", ok=False).err
    assert "handoff new" in run(capsys, "new", "a", "c", "handoff", ok=False).err
    assert "inválido" in run(capsys, "new", "a/b", "c", ok=False).err


# ──────────────────────────────────────────────────────────────── close / reopen

def test_close_corta_con_pendientes_y_force(store, repo, capsys):
    run(capsys, "new", "a", "b")
    fill(store, "2026-03-10_a_b", "- [ ]", "- [ ] revisar el cron")
    err = run(capsys, "close", "2026-03-10_a_b", ok=False).err
    assert "1 pendiente(s)" in err and "revisar el cron" in err
    assert docs.find(store, "2026-03-10_a_b").scope == "wip"
    err = run(capsys, "close", "2026-03-10_a_b", "--force").err
    assert "Cerrado con 1 pendiente" in err


def test_close_y_reopen(store, repo, capsys):
    run(capsys, "new", "a", "plan1", "plan")
    with store.writer() as w:
        w.emit("set_state", docs.find(store, f"{HOY}_a_plan1").id, {"set": {"nota": "baja prioridad"}})
    r = run(capsys, "close", f"{HOY}_a_plan1")
    assert "Nota descartada" in r.err
    doc = docs.find(store, f"{HOY}_a_plan1")
    assert (doc.scope, doc.estado, doc.meta["cerrado"]) == ("done", "completado", HOY)
    assert "nota" not in doc.meta
    assert "ya está cerrado" in run(capsys, "close", doc.name, ok=False).err

    r = run(capsys, "reopen", doc.name, "volvió a pasar")
    doc = docs.find(store, doc.name)
    assert (doc.scope, doc.estado, doc.meta["reabierto"], doc.meta["nota"]) == \
        ("wip", "en_progreso", HOY, "volvió a pasar")
    assert "cerrado" not in doc.meta
    assert "no está cerrado" in run(capsys, "reopen", doc.name, ok=False).err
    assert "Sin motivo" in (run(capsys, "close", doc.name) and run(capsys, "reopen", doc.name).err)


def test_ref_no_se_cierra(store, repo, capsys):
    run(capsys, "new", "a", "r", "ref")
    assert "no se cierra" in run(capsys, "close", "a_r", ok=False).err


# ──────────────────────────────────────────────────────────────── promote

def test_promote_y_links_que_siguen_al_doc(store, repo, capsys):
    run(capsys, "new", "bcra", "rate_limit", "bug")
    name = f"{HOY}_bcra_BUG_rate_limit"
    assert "Nada para promover" in run(capsys, "promote", name, "bcra", "limites", ok=False).err
    assert not docs.name_taken(store, "bcra_limites")

    fill(store, name, "# para retomar o re-diagnosticar", "curl -I https://api.example.com")
    r = run(capsys, "promote", name, "bcra", "limites")
    assert "llevadas: Comandos útiles de referencia" in r.out
    assert "Contexto / topología (vacía)" in r.err
    ref = docs.find(store, "bcra_limites")
    meta, body = parse_front_matter(docs.render(store, ref))
    assert meta["origen"] == [f"../_wip/{name}.md"]
    assert meta["repo"] == "repo"
    assert "curl -I" in body and "## Contexto" not in body
    assert "## Contenido" not in body          # la ref creada por promote no lleva el stub
    dmeta, _ = parse_front_matter(docs.render(store, docs.find(store, name)))
    assert dmeta["referencias"] == ["../_ref/bcra_limites.md"]

    assert "--force" in run(capsys, "promote", name, "bcra", "limites", ok=False).err

    run(capsys, "close", name)                 # los links se calculan: siguen al doc
    meta, _ = parse_front_matter(docs.render(store, docs.find(store, "bcra_limites")))
    assert meta["origen"] == [f"../_done/{name}.md"]
    ops = [r[0] for r in store.projection.db.execute(
        "SELECT op FROM revisions WHERE doc_id = ? ORDER BY seq", (ref.id,))]
    assert ops == ["create", "promote"]


def test_promote_seccion_y_agregar(store, repo, capsys):
    run(capsys, "new", "bcra", "cache", "plan")
    plan = f"{HOY}_bcra_cache"
    fill(store, plan, "<!-- La solución, con fases si el trabajo es incremental. -->", "Cache LRU.")
    run(capsys, "new", "bcra", "limites", "ref")
    run(capsys, "promote", plan, "bcra", "limites", "--seccion", "diseño")
    ref = docs.find(store, "bcra_limites")
    assert "## Contenido" in ref.body            # la existente conserva lo suyo
    assert ref.body.rstrip().endswith("Cache LRU.")
    assert ref.links == [["origen", docs.find(store, plan).id]]


# ──────────────────────────────────────────────────────────────── listados y búsqueda

@pytest.fixture
def poblado(store, repo, capsys):
    run(capsys, "new", "api", "timeout", "bug")
    name = f"{HOY}_api_BUG_timeout"
    tldr = "<!-- 2-4 líneas: problema, causa raíz, solución. La 1ª dice QUÉ se hizo (sale en weekly). -->"
    fill(store, name, tldr,
         "Se subió el timeout a 30 s. El server tardaba más en fin de mes. Tercera oración.")
    run(capsys, "close", name, "--force")
    run(capsys, "new", "api", "endpoints", "ref")
    run(capsys, "new", "api", "abierto")
    return store


def test_list(poblado, capsys):
    out = run(capsys, "list", "all").out
    assert f"{HOY}_api_BUG_timeout" in out and "api_endpoints" in out and f"{HOY}_api_abierto" in out
    assert run(capsys, "list").out.strip() == f"{HOY}_api_abierto"
    assert run(capsys, "list", "done").out.strip() == f"{HOY}_api_BUG_timeout"


def test_search(poblado, capsys):
    out = run(capsys, "search", "TIMEOUT a 30").out
    assert "1 doc(s) de 3" in out
    assert "cerrado 10 mar" in out and f"_done/{HOY}_api_BUG_timeout.md" in out
    assert "Se subió el timeout a 30 s." in out
    assert "━━━━━  repo  (1)" in out
    assert "sin coincidencias" in run(capsys, "search", "timeout", "ref").out
    assert "Regex inválida" in run(capsys, "search", "(", ok=False).err
    out = run(capsys, "search", "--fts", "tardaba").out
    assert f"_done/{HOY}_api_BUG_timeout.md" in out
    assert "inválida" in run(capsys, "search", "--fts", '"', ok=False).err


def test_weekly(poblado, capsys, monkeypatch):
    out = run(capsys, "weekly").out
    assert "1 trabajos cerrados" in out and "[bug]   1 bugs" in out
    assert "Se subió el timeout a 30 s. El server tardaba más en fin de mes." in out
    assert "Tercera" not in out
    assert "Tercera oración." in run(capsys, "weekly", "7", "full").out
    monkeypatch.setenv("CAIRN_NOW", "2026-04-10T12:00:00+00:00")
    assert "0 trabajos cerrados" in run(capsys, "weekly").out


def test_check(poblado, capsys):
    assert "[OK] todo en orden" in run(capsys, "check").out
    with poblado.writer() as w:
        w.emit("set_state", docs.find(poblado, f"{HOY}_api_abierto").id, {"set": {"estado": "raro"}})
    out = run(capsys, "check", ok=False).out
    assert "estado 'raro'" in out


# ──────────────────────────────────────────────────────────────── handoffs

def hook_ctx(capsys, monkeypatch, cwd, source="startup", raw=None):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(raw if raw is not None else
                                                 json.dumps({"cwd": str(cwd), "source": source})))
    out = run(capsys, "handoff", "hook").out
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def test_handoff_new_take_archiva(store, repo, capsys):
    run(capsys, "new", "repo", "feature", "plan")
    out = run(capsys, "handoff", "new", "repo", "siguientes", "--plan", f"{HOY}_repo_feature.md").out
    name = out.strip()
    assert name == f"{HOY}_repo_HANDOFF_siguientes"
    h = docs.find(store, name)
    meta, _ = parse_front_matter(docs.render(store, h))
    assert meta["plan"] == [f"../_wip/{HOY}_repo_feature.md"]
    assert (meta["tipo"], meta["estado"]) == ("handoff", "pendiente")
    assert f"handoffs pendientes:\n  {name}" in run(capsys, "list").out

    out = run(capsys, "handoff", "take").out
    assert f"Handoff tomado: {name}" in out and "## Leer primero" in out
    h = docs.find(store, name)
    assert (h.scope, h.estado, h.meta["usada"]) == ("done", "usada", HOY)
    assert "ya no está pendiente" in run(capsys, "handoff", "take", name, ok=False).err
    # tomado por error: vuelve a pendiente
    run(capsys, "reopen", name)
    h = docs.find(store, name)
    assert (h.scope, h.estado) == ("wip", "pendiente") and "usada" not in h.meta


def test_handoff_drop_y_close_no_vale(store, repo, capsys):
    name = run(capsys, "handoff", "new", "repo", "x").out.strip()
    assert "se archiva con `handoff take`" in run(capsys, "close", name, ok=False).err
    run(capsys, "handoff", "drop", name, "ya no aplica")
    h = docs.find(store, name)
    assert (h.estado, h.meta["descartada"], h.meta["nota"]) == ("descartada", HOY, "ya no aplica")
    assert "Sin handoffs pendientes" in run(capsys, "handoff", "list").out
    assert "no es un handoff" in (run(capsys, "new", "a", "b") and
                                  run(capsys, "handoff", "drop", f"{HOY}_a_b", ok=False).err)


def test_handoff_take_sin_nombre_con_varias_o_ninguna(store, repo, capsys):
    assert "No hay handoffs pendientes para repo" in run(capsys, "handoff", "take", ok=False).err
    run(capsys, "handoff", "new", "repo", "a")
    run(capsys, "handoff", "new", "repo", "b")
    err = run(capsys, "handoff", "take", ok=False).err
    assert "Hay 2 handoffs pendientes para repo; decí cuál" in err


def test_handoff_hook_segun_repo_y_origen(store, repo, env, capsys, monkeypatch):
    otro = env / "projects" / "otro"
    otro.mkdir()
    subprocess.run(["git", "init", "-q", str(otro)], check=True)
    assert hook_ctx(capsys, monkeypatch, repo) == ""
    run(capsys, "handoff", "new", "repo", "a")
    run(capsys, "handoff", "new", "x", "cruzado", "--para", "repo", "--para", "otro")
    ctx = hook_ctx(capsys, monkeypatch, repo / ".")
    assert "2 handoff(s) pendiente(s) para repo" in ctx and "preguntale si la toma" in ctx
    assert "cairn handoff take" in ctx
    assert "1 handoff(s) pendiente(s) para otro" in hook_ctx(capsys, monkeypatch, otro)
    assert "2 handoff(s) pendiente(s) para la raíz" in hook_ctx(capsys, monkeypatch, env / "projects")
    assert hook_ctx(capsys, monkeypatch, repo, source="resume") == ""
    assert hook_ctx(capsys, monkeypatch, repo, source="compact") == ""
    assert "handoff(s)" in hook_ctx(capsys, monkeypatch, repo, source="clear")
    assert "2 handoff(s)" in hook_ctx(capsys, monkeypatch, repo, raw="no es json")


def test_handoff_hook_sin_init_no_rompe(env, capsys, monkeypatch):
    assert hook_ctx(capsys, monkeypatch, env) == ""


def test_handoff_hook_en_worktree_fuera_de_projects(store, repo, tmp_path_factory, capsys, monkeypatch):
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    wt = tmp_path_factory.mktemp("afuera") / "wt"
    subprocess.run([*git, "worktree", "add", "-q", str(wt)], check=True)
    run(capsys, "handoff", "new", "repo", "a")
    assert "para repo" in hook_ctx(capsys, monkeypatch, wt)


def test_handoff_viejo_y_plan_modificado(store, repo, capsys, monkeypatch):
    run(capsys, "new", "repo", "feature", "plan")
    monkeypatch.setenv("CAIRN_NOW", "2026-03-10T12:00:00+00:00")
    name = run(capsys, "handoff", "new", "repo", "a", "--plan", f"{HOY}_repo_feature").out.strip()
    monkeypatch.setenv("CAIRN_NOW", "2026-03-28T12:00:00+00:00")
    fill(store, f"{HOY}_repo_feature", "- [ ]", "- [ ] algo")      # el plan se tocó después
    assert "hace 18 día(s); VIEJA" in run(capsys, "handoff", "list").out
    assert "sin tomar hace 18 días" in run(capsys, "check", ok=False).out
    out = run(capsys, "handoff", "take", name).out
    assert "puede haber quedado vieja" in out
    assert "se modificó después de escrita la nota" in out


def test_find_por_id_y_ambiguo(store, repo, capsys):
    run(capsys, "new", "a", "b")
    doc = docs.find(store, f"{HOY}_a_b")
    assert docs.find(store, doc.id[:8]).id == doc.id
    assert docs.find(store, f"../_wip/{HOY}_a_b.md").id == doc.id
    with pytest.raises(CairnError, match="No encontrado"):
        docs.find(store, "nada")
