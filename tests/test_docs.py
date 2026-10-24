"""Parity with wip-doc: the same cases as its tests, against the event store."""

import json
import subprocess

import pytest

from cairn import docs, edit
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
    """Replaces text in the body (with a hand-made event, from before `edit` existed)."""
    doc = docs.find(store, ref)
    assert old in doc.body, old
    with store.writer() as w:
        w.emit("edit", doc.id, {"body": doc.body.replace(old, new, 1)})


@pytest.fixture
def repo(env, monkeypatch):
    """A git repo under the projects root, as the PWD."""
    r = env / "projects" / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    monkeypatch.chdir(r)
    return r


# ──────────────────────────────────────────────────────────────── new

def test_new_stamps_the_pwd_repo(store, repo, capsys):
    out = run(capsys, "new", "bcra", "rate_limit", "bug").out
    assert out.strip() == f"{HOY}_bcra_BUG_rate_limit"
    doc = docs.find(store, f"{HOY}_bcra_BUG_rate_limit.md")
    assert doc.meta == {"type": "bug", "repo": "repo", "status": "in_progress", "date": HOY,
                        "author": "Pablo + Claude"}
    assert doc.scope == "wip" and doc.title == "bcra — rate limit"
    assert "## Discoveries (in order)" in doc.body


def test_new_ref_without_date_and_plan(store, repo, capsys):
    run(capsys, "new", "bcra", "endpoints", "ref")
    ref = docs.find(store, "bcra_endpoints")
    assert ref.scope == "ref" and ref.meta["updated"] == HOY and "status" not in ref.meta
    assert "## Content" in ref.body
    run(capsys, "new", "bcra", "cache", "plan")
    assert "## Design / proposal" in docs.find(store, f"{HOY}_bcra_cache").body


def test_new_cross_repo_and_explicit_repo(store, env, monkeypatch, capsys):
    monkeypatch.chdir(env / "projects")
    run(capsys, "new", "infra", "vpn")
    assert docs.find(store, f"{HOY}_infra_vpn").meta["repo"] == "cross-repo"
    run(capsys, "new", "infra", "dns", "--repo", "cross-repo")
    assert docs.find(store, f"{HOY}_infra_dns").meta["repo"] == "cross-repo"
    # an older doc created outside any repo has no `repo:` at all: it groups with the rest
    old = docs.new(store, "infra", "viejo").doc
    edit.apply(store, old.id, unset=["repo"])
    assert "repo" not in docs.get(store, old.id).meta
    out = docs.grouped_by_repo(docs.all_docs(store, ("wip",)))
    assert out[0].startswith("3 doc(s) in 1 repo(s)") and "cross-repo (3)" in out


def test_new_does_not_overwrite_and_validates(store, repo, capsys):
    run(capsys, "new", "a", "b")
    assert "Already exists" in run(capsys, "new", "a", "b", ok=False).err
    assert "Invalid type" in run(capsys, "new", "a", "c", "otro", ok=False).err
    assert "handoff new" in run(capsys, "new", "a", "c", "handoff", ok=False).err
    assert "Invalid name" in run(capsys, "new", "a/b", "c", ok=False).err


# ──────────────────────────────────────────────────────────────── close / reopen

def test_close_stops_on_pending_items_and_force(store, repo, capsys):
    run(capsys, "new", "a", "b")
    fill(store, "2026-03-10_a_b", "- [ ]", "- [ ] revisar el cron")
    err = run(capsys, "close", "2026-03-10_a_b", ok=False).err
    assert "1 unchecked pending item(s)" in err and "revisar el cron" in err
    assert docs.find(store, "2026-03-10_a_b").scope == "wip"
    err = run(capsys, "close", "2026-03-10_a_b", "--force").err
    assert "Closed with 1 unchecked pending" in err


def test_close_discard(store, repo, capsys):
    run(capsys, "new", "a", "plan1", "plan")
    fill(store, f"{HOY}_a_plan1", "- [ ]", "- [ ] fase 2")
    assert "--discard" in run(capsys, "close", f"{HOY}_a_plan1", ok=False).err
    assert "needs a reason" in run(capsys, "close", f"{HOY}_a_plan1", "--discard", " ", ok=False).err

    r = run(capsys, "close", f"{HOY}_a_plan1", "--discard", "se rediseña entero")
    assert "Closed with" not in r.err
    doc = docs.find(store, f"{HOY}_a_plan1")
    assert (doc.scope, doc.status, doc.meta["closed"], doc.meta["note"]) == \
        ("done", "discarded", HOY, "se rediseña entero")

    out = run(capsys, "weekly").out
    assert "(1 discarded)" in out and "discarded" in out.split(doc.title)[1].splitlines()[0]
    assert not docs.check(store)

    run(capsys, "reopen", doc.name, "se retoma")
    doc = docs.find(store, doc.name)
    assert (doc.scope, doc.status, doc.meta["note"]) == ("wip", "in_progress", "se retoma")


def test_close_and_reopen(store, repo, capsys):
    run(capsys, "new", "a", "plan1", "plan")
    with store.writer() as w:
        w.emit("set_state", docs.find(store, f"{HOY}_a_plan1").id, {"set": {"note": "baja prioridad"}})
    r = run(capsys, "close", f"{HOY}_a_plan1")
    assert "Note dropped" in r.err
    doc = docs.find(store, f"{HOY}_a_plan1")
    assert (doc.scope, doc.status, doc.meta["closed"]) == ("done", "completed", HOY)
    assert "note" not in doc.meta
    assert "is already closed" in run(capsys, "close", doc.name, ok=False).err

    r = run(capsys, "reopen", doc.name, "volvió a pasar")
    doc = docs.find(store, doc.name)
    assert (doc.scope, doc.status, doc.meta["reopened"], doc.meta["note"]) == \
        ("wip", "in_progress", HOY, "volvió a pasar")
    assert "closed" not in doc.meta
    assert "is not closed" in run(capsys, "reopen", doc.name, ok=False).err
    assert "No reason" in (run(capsys, "close", doc.name) and run(capsys, "reopen", doc.name).err)


def test_ref_is_not_closed(store, repo, capsys):
    run(capsys, "new", "a", "r", "ref")
    assert "is not closed" in run(capsys, "close", "a_r", ok=False).err


# ──────────────────────────────────────────────────────────────── promote

def test_promote_and_links_that_follow_the_doc(store, repo, capsys):
    run(capsys, "new", "bcra", "rate_limit", "bug")
    name = f"{HOY}_bcra_BUG_rate_limit"
    assert "Nothing to promote" in run(capsys, "promote", name, "bcra", "limites", ok=False).err
    assert not docs.name_taken(store, "bcra_limites")

    fill(store, name, "# to pick it up again or diagnose it again", "curl -I https://api.example.com")
    r = run(capsys, "promote", name, "bcra", "limites")
    assert "carried over: Useful commands" in r.out
    assert "Context / topology (empty)" in r.err
    ref = docs.find(store, "bcra_limites")
    meta, body = parse_front_matter(docs.render(store, ref))
    assert meta["origin"] == [f"../_wip/{name}.md"]
    assert meta["repo"] == "repo"
    assert "curl -I" in body and "## Contexto" not in body
    assert "## Content" not in body          # the ref created by promote does not carry the stub
    dmeta, _ = parse_front_matter(docs.render(store, docs.find(store, name)))
    assert dmeta["references"] == ["../_ref/bcra_limites.md"]

    assert "--force" in run(capsys, "promote", name, "bcra", "limites", ok=False).err

    run(capsys, "close", name)                 # links are computed: they follow the doc
    meta, _ = parse_front_matter(docs.render(store, docs.find(store, "bcra_limites")))
    assert meta["origin"] == [f"../_done/{name}.md"]
    ops = [r[0] for r in store.projection.db.execute(
        "SELECT op FROM revisions WHERE doc_id = ? ORDER BY seq", (ref.id,))]
    assert ops == ["create", "promote"]


def test_promote_section_and_append(store, repo, capsys):
    run(capsys, "new", "bcra", "cache", "plan")
    plan = f"{HOY}_bcra_cache"
    fill(store, plan, "<!-- The solution, in phases if the work is incremental. -->", "Cache LRU.")
    run(capsys, "new", "bcra", "limites", "ref")
    run(capsys, "promote", plan, "bcra", "limites", "--section", "design")
    ref = docs.find(store, "bcra_limites")
    assert "## Content" in ref.body            # the existing one keeps its content
    assert ref.body.rstrip().endswith("Cache LRU.")
    assert ref.links == [["origin", docs.find(store, plan).id]]


# ──────────────────────────────────────────────────────────────── link

def test_link_by_hand(store, repo, capsys):
    run(capsys, "new", "api", "viejo", "plan")
    run(capsys, "new", "api", "nuevo", "plan")
    viejo, nuevo = f"{HOY}_api_viejo", f"{HOY}_api_nuevo"
    assert f"Linked: {nuevo} origin → {viejo}" in run(capsys, "link", nuevo, "origin", viejo).out
    meta, _ = parse_front_matter(docs.render(store, docs.find(store, nuevo)))
    assert meta["origin"] == [f"../_wip/{viejo}.md"]
    assert docs.find(store, viejo).links == []                # one way

    r = run(capsys, "link", nuevo, "origin", viejo)           # again: nothing to do
    assert r.out == "" and "already had" in r.err
    assert "itself" in run(capsys, "link", nuevo, "plan", nuevo, ok=False).err
    assert "Not found" in run(capsys, "link", nuevo, "plan", "no_existe", ok=False).err
    assert "has no `plan` link" in run(capsys, "link", nuevo, "plan", viejo, "--remove", ok=False).err
    assert docs.link(store, nuevo, "origen", viejo).extra["changed"] is False   # old vocabulary

    assert "Unlinked" in run(capsys, "link", nuevo, "origin", viejo, "--remove").out
    assert docs.find(store, nuevo).links == []
    ops = [r[0] for r in store.projection.db.execute(
        "SELECT op FROM revisions WHERE doc_id = ? ORDER BY seq", (docs.find(store, nuevo).id,))]
    assert ops == ["create", "link", "unlink"]


def test_link_remove_dangling(store, repo, capsys):
    """A link to a doc that does not exist (what `check` reports) is removed by its id prefix."""
    run(capsys, "new", "api", "huerfano", "plan")
    doc = docs.find(store, f"{HOY}_api_huerfano")
    with store.writer() as w:
        w.emit("link", doc.id, {"link_add": [["plan", "ab" * 16]]})
    assert any("does not exist" in p for p in docs.check(store))
    assert "has no `origin`" not in run(capsys, "link", doc.name, "plan", "abababab", "--remove").err
    assert docs.find(store, doc.name).links == []
    assert "Not found" in run(capsys, "link", doc.name, "plan", "abababab", ok=False).err


# ──────────────────────────────────────────────────────────────── rename / delete / restore

def test_rename_updates_mentions_and_keeps_links(store, repo, capsys):
    run(capsys, "new", "taller", "producto", "plan")
    run(capsys, "new", "juego", "mesa", "plan")
    run(capsys, "new", "taller", "producto_v2", "plan")
    old, new, mesa, v2 = (f"{HOY}_taller_producto", f"{HOY}_zunft_producto", f"{HOY}_juego_mesa",
                          f"{HOY}_taller_producto_v2")
    run(capsys, "link", mesa, "plan", old)
    fill(store, old, "## TL;DR", f"## TL;DR\n\nEste es `{old}`.")
    fill(store, mesa, "## TL;DR", f"## TL;DR\n\nVer ../_wip/{old}.md y {old}@~1; no {v2} ni {old}.v2.")
    edit.apply(store, mesa, set_={"note": f"Reemplazado por {old}"})

    out = run(capsys, "rename", old, f"{new}.md").out
    assert f"Renamed: {old} → {new}" in out and f"mentions updated in: {mesa}" in out
    doc = docs.find(store, new)
    assert f"Este es `{new}`." in doc.body
    m = docs.find(store, mesa)
    assert f"../_wip/{new}.md y {new}@~1; no {v2} ni {old}.v2." in m.body   # whole tokens only
    assert m.meta["note"] == f"Reemplazado por {new}"
    assert m.links == [["plan", doc.id]]
    assert f"../_wip/{new}.md" in docs.render(store, m)
    assert docs.find(store, old).id == doc.id                 # the old name still finds it
    assert docs.find(store, v2).name == v2                    # untouched
    ops = [r[0] for r in store.projection.db.execute(
        "SELECT op FROM revisions WHERE doc_id = ? ORDER BY seq", (doc.id,))]
    assert ops[-1] == "rename"
    assert docs.check(store) == []


def test_rename_refusals(store, repo, capsys):
    run(capsys, "new", "api", "a", "plan")
    run(capsys, "new", "api", "b", "plan")
    run(capsys, "new", "api", "endpoints", "ref")
    a, b = f"{HOY}_api_a", f"{HOY}_api_b"
    assert "Already exists" in run(capsys, "rename", a, b, ok=False).err
    assert "already called that" in run(capsys, "rename", a, a, ok=False).err
    assert "YYYY-MM-DD_" in run(capsys, "rename", a, "api_a", ok=False).err
    assert "Invalid name" in run(capsys, "rename", a, f"{HOY} api a", ok=False).err
    run(capsys, "rename", "api_endpoints", "api_rutas")         # a reference goes without a date
    assert docs.find(store, "api_rutas").scope == "ref"


def test_delete_and_restore(store, repo, capsys):
    run(capsys, "new", "api", "duplicado", "plan")
    run(capsys, "new", "api", "plan", "plan")
    dup, plan = f"{HOY}_api_duplicado", f"{HOY}_api_plan"
    run(capsys, "link", plan, "origin", dup)
    doc_id = docs.find(store, dup).id

    r = run(capsys, "delete", dup, "creado por error")
    assert f"Deleted: {dup}" in r.out and plan in r.err       # it warns about who links to it
    assert "Not found" in run(capsys, "show", dup, ok=False).err
    assert dup not in run(capsys, "list", "all").out
    assert run(capsys, "search", "duplicado").out.count(dup) == 0
    assert [p for p in docs.check(store) if "was deleted" in p and f"restore {dup}" in p]
    assert f"{dup}  [plan · deleted" in run(capsys, "restore").out
    assert "creado por error" in run(capsys, "restore").out

    # its name is free; restoring needs it back
    run(capsys, "new", "api", "duplicado", "plan")
    assert "taken by another doc" in run(capsys, "restore", dup, ok=False).err
    run(capsys, "rename", dup, f"{HOY}_api_duplicado_nuevo")
    assert f"Restored: {dup}" in run(capsys, "restore", dup).out
    doc = docs.find(store, dup)
    assert doc.id == doc_id and "deleted" not in doc.meta and doc.scope == "wip"
    assert docs.check(store) == []
    assert "No deleted docs" in run(capsys, "restore").out
    assert "No deleted doc is called" in run(capsys, "restore", dup, ok=False).err


def test_delete_survives_rebuild_and_is_not_edited(store, repo, capsys):
    run(capsys, "new", "api", "x", "plan")
    name = f"{HOY}_api_x"
    with pytest.raises(CairnError, match="not deleted .* by editing"):
        edit.apply(store, name, set_={"deleted": HOY})
    run(capsys, "delete", name)
    store.rebuild()
    assert [d.name for d in docs.deleted_docs(store)] == [name]
    assert not docs.name_taken(store, name)
    run(capsys, "restore", name)
    assert docs.find(store, name).meta.get("deleted") is None


# ──────────────────────────────────────────────────────────────── listings and search

@pytest.fixture
def poblado(store, repo, capsys):
    run(capsys, "new", "api", "timeout", "bug")
    name = f"{HOY}_api_BUG_timeout"
    tldr = ("<!-- 2-4 lines: problem, root cause, fix. The first one says WHAT was done (it goes in "
            "weekly). -->")
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


def test_list_by_repo(poblado, capsys, monkeypatch):
    run(capsys, "new", "otra", "cosa", "plan", "--repo", "otro")
    run(capsys, "new", "web", "suelto", "plan", "--repo", "cross-repo")
    monkeypatch.setenv("CAIRN_NOW", "2026-03-15T12:00:00+00:00")
    run(capsys, "handoff", "new", "api", "seguir")
    out = run(capsys, "list", "--by-repo").out
    assert out.splitlines()[0].startswith("3 doc(s) in 3 repo(s)")
    assert "repo (1)\n" in out and "otro (1)\n" in out and "cross-repo (1)\n" in out
    assert (f"  {HOY}_api_abierto  [infra · open · modified {HOY}, 5 day(s) ago]") in out
    assert "pending handoffs:\n  2026-03-15_api_HANDOFF_seguir" in out
    fill(poblado, f"{HOY}_otra_cosa", "## TL;DR", "## TL;DR\n\nalgo")     # the last one touched
    grupos = [ln for ln in run(capsys, "list", "--by-repo").out.splitlines() if ln.endswith("(1)")]
    assert grupos[0] == "otro (1)"
    assert "modified 2026-03-15, today" in run(capsys, "list", "--by-repo").out
    assert run(capsys, "list").out.splitlines()[0] == f"{HOY}_otra_cosa"   # the flat list does not change


def test_search(poblado, capsys):
    out = run(capsys, "search", "TIMEOUT a 30").out
    assert "1 doc(s) of 3" in out
    assert "closed 10 Mar" in out and f"_done/{HOY}_api_BUG_timeout.md" in out
    assert "Se subió el timeout a 30 s." in out
    assert "━━━━━  repo  (1)" in out
    assert "no matches" in run(capsys, "search", "timeout", "ref").out
    assert "Invalid regex" in run(capsys, "search", "(", ok=False).err
    out = run(capsys, "search", "--fts", "tardaba").out
    assert f"_done/{HOY}_api_BUG_timeout.md" in out
    assert "«tardaba»" in out and "1 doc(s), by relevance\n" in out      # the snippet, not the TL;DR
    hits, total = docs.search_fts(poblado, "api", limit=2)                 # all three docs are api's
    assert len(hits) == 2 and total == 3
    assert "showing the 2 most relevant" in docs.fts_header("api", hits, total)
    assert "Invalid FTS query" in run(capsys, "search", "--fts", '"', ok=False).err


def test_weekly(poblado, capsys, monkeypatch):
    out = run(capsys, "weekly").out
    assert "1 pieces of work closed" in out and "[bug]   1 bugs" in out
    assert "Se subió el timeout a 30 s. El server tardaba más en fin de mes." in out
    assert "Tercera" not in out
    assert "Tercera oración." in run(capsys, "weekly", "7", "full").out
    monkeypatch.setenv("CAIRN_NOW", "2026-04-10T12:00:00+00:00")
    assert "0 pieces of work closed" in run(capsys, "weekly").out


def test_check(poblado, capsys):
    assert "[OK] all in order" in run(capsys, "check").out
    with poblado.writer() as w:
        w.emit("set_state", docs.find(poblado, f"{HOY}_api_abierto").id, {"set": {"status": "raro"}})
    out = run(capsys, "check", ok=False).out
    assert "status 'raro'" in out


# ──────────────────────────────────────────────────────────────── handoffs

def hook_ctx(capsys, monkeypatch, cwd, source="startup", raw=None):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(raw if raw is not None else
                                                 json.dumps({"cwd": str(cwd), "source": source})))
    out = run(capsys, "handoff", "hook").out
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def test_handoff_new_and_take_archives(store, repo, capsys):
    run(capsys, "new", "repo", "feature", "plan")
    out = run(capsys, "handoff", "new", "repo", "siguientes", "--plan", f"{HOY}_repo_feature.md").out
    name = out.strip()
    assert name == f"{HOY}_repo_HANDOFF_siguientes"
    h = docs.find(store, name)
    meta, _ = parse_front_matter(docs.render(store, h))
    assert meta["plan"] == [f"../_wip/{HOY}_repo_feature.md"]
    assert (meta["type"], meta["status"]) == ("handoff", "pending")
    assert f"pending handoffs:\n  {name}" in run(capsys, "list").out

    out = run(capsys, "handoff", "take").out
    assert f"Handoff taken: {name}" in out and "## Read first" in out
    h = docs.find(store, name)
    assert (h.scope, h.status, h.meta["taken"]) == ("done", "taken", HOY)
    assert "is no longer pending" in run(capsys, "handoff", "take", name, ok=False).err
    # taken by mistake: back to pending
    run(capsys, "reopen", name)
    h = docs.find(store, name)
    assert (h.scope, h.status) == ("wip", "pending") and "taken" not in h.meta


def test_handoff_drop_and_close_is_refused(store, repo, capsys):
    name = run(capsys, "handoff", "new", "repo", "x").out.strip()
    assert "archived with `handoff take`" in run(capsys, "close", name, ok=False).err
    run(capsys, "handoff", "drop", name, "ya no aplica")
    h = docs.find(store, name)
    assert (h.status, h.meta["dropped"], h.meta["note"]) == ("dropped", HOY, "ya no aplica")
    assert "No pending handoffs" in run(capsys, "handoff", "list").out
    assert "is not a handoff" in (run(capsys, "new", "a", "b") and
                                  run(capsys, "handoff", "drop", f"{HOY}_a_b", ok=False).err)


def test_handoff_take_without_name_with_several_or_none(store, repo, capsys):
    assert "No pending handoffs for repo" in run(capsys, "handoff", "take", ok=False).err
    run(capsys, "handoff", "new", "repo", "a")
    run(capsys, "handoff", "new", "repo", "b")
    err = run(capsys, "handoff", "take", ok=False).err
    assert "There are 2 pending handoffs for repo; say which one" in err


def test_handoff_hook_by_repo_and_source(store, repo, env, capsys, monkeypatch):
    otro = env / "projects" / "otro"
    otro.mkdir()
    subprocess.run(["git", "init", "-q", str(otro)], check=True)
    assert hook_ctx(capsys, monkeypatch, repo) == ""
    run(capsys, "handoff", "new", "repo", "a")
    run(capsys, "handoff", "new", "x", "cruzado", "--to", "repo", "--to", "otro")
    ctx = hook_ctx(capsys, monkeypatch, repo / ".")
    assert "2 pending handoff(s) for repo" in ctx and "ask whether to take it" in ctx
    assert "cairn handoff take" in ctx
    assert "1 pending handoff(s) for otro" in hook_ctx(capsys, monkeypatch, otro)
    assert "2 pending handoff(s) for the projects root" in hook_ctx(capsys, monkeypatch, env / "projects")
    assert hook_ctx(capsys, monkeypatch, repo, source="resume") == ""
    assert hook_ctx(capsys, monkeypatch, repo, source="compact") == ""
    assert "handoff(s)" in hook_ctx(capsys, monkeypatch, repo, source="clear")
    assert "2 pending handoff(s)" in hook_ctx(capsys, monkeypatch, repo, raw="no es json")


def test_handoff_hook_without_init_does_not_break(env, capsys, monkeypatch):
    assert hook_ctx(capsys, monkeypatch, env) == ""


def test_handoff_hook_in_worktree_outside_projects(store, repo, tmp_path_factory, capsys, monkeypatch):
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    wt = tmp_path_factory.mktemp("afuera") / "wt"
    subprocess.run([*git, "worktree", "add", "-q", str(wt)], check=True)
    run(capsys, "handoff", "new", "repo", "a")
    assert "for repo" in hook_ctx(capsys, monkeypatch, wt)


def test_stale_handoff_and_modified_plan(store, repo, capsys, monkeypatch):
    run(capsys, "new", "repo", "feature", "plan")
    monkeypatch.setenv("CAIRN_NOW", "2026-03-10T12:00:00+00:00")
    name = run(capsys, "handoff", "new", "repo", "a", "--plan", f"{HOY}_repo_feature").out.strip()
    monkeypatch.setenv("CAIRN_NOW", "2026-03-28T12:00:00+00:00")
    fill(store, f"{HOY}_repo_feature", "- [ ]", "- [ ] algo")      # the plan was touched afterwards
    assert "18 day(s) old; OLD" in run(capsys, "handoff", "list").out
    assert "not taken for 18 days" in run(capsys, "check", ok=False).out
    out = run(capsys, "handoff", "take", name).out
    assert "may be out of date" in out
    assert "changed after the note was written" in out


def test_find_by_id_and_ambiguous(store, repo, capsys):
    run(capsys, "new", "a", "b")
    doc = docs.find(store, f"{HOY}_a_b")
    assert docs.find(store, doc.id[:8]).id == doc.id
    assert docs.find(store, f"../_wip/{HOY}_a_b.md").id == doc.id
    with pytest.raises(CairnError, match="Not found"):
        docs.find(store, "nada")
