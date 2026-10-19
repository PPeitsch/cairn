"""`cairn migrate wip-doc`: import and export of the file tree, leftovers and the hook."""

import os
from pathlib import Path

import pytest

from cairn import config, docs, tree, vocab
from cairn.cli import main
from cairn.markdown import dump_front_matter, parse_front_matter
from cairn.migrate import wip_doc
from cairn.store import Store

HOY = "2026-03-10"
IMPORT = ("migrate", "wip-doc", "import")
EXPORT = ("migrate", "wip-doc", "export")


def run(capsys, *args, ok=True):
    code = main(list(args))
    out = capsys.readouterr()
    assert (code == 0) == ok, out
    return out


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
    """A made-up wip-doc tree: docs with links, a handoff, junk and a broken link."""
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
    out = run(capsys, *IMPORT, str(arbol), "--dry-run")
    assert "Would import: 4 doc(s), 3 link(s)" in out.out
    assert store.projection.count_events() == 0
    out = run(capsys, *IMPORT, str(arbol))
    assert "Imported: 4 doc(s), 3 link(s)" in out.out
    assert "viejo_sin_front_matter.md: no front-matter" in out.err
    assert "api_limites: origin → ../_done/no_existe.md" in out.err
    assert "3 file(s) that are not docs" in out.err

    bug = docs.find(store, "2026-01-05_api_BUG_timeout")
    ref = docs.find(store, "api_limites")
    h = docs.find(store, "2026-03-09_api_HANDOFF_seguir")
    assert bug.scope == "done" and ref.scope == "ref" and h.scope == "wip"
    assert bug.links == [["references", ref.id]]
    assert ref.links == [["origin", bug.id]] and ref.meta["origin"] == ["../_done/no_existe.md"]
    assert h.links == [["plan", docs.find(store, "2026-03-01_api_cache").id]]
    assert bug.last_event.startswith("2023-11-14")           # the file's mtime
    ops = {r[0] for r in store.projection.db.execute("SELECT op FROM events")}
    assert ops == {"import"}
    # wip-doc's Spanish front-matter goes into the events in the canonical vocabulary
    payloads = [r[0] for r in store.projection.db.execute("SELECT payload FROM events")]
    assert not any('"tipo"' in x or '"estado"' in x or '"origen"' in x for x in payloads)
    assert (bug.meta["status"], bug.meta["closed"]) == ("resolved", "2026-03-08")

    # twice does not duplicate
    out = run(capsys, *IMPORT, str(arbol))
    assert "Imported: 0 doc(s)" in out.out and "already in the database" in out.err
    assert "[OK] all in order" in run(capsys, "check").out


def test_import_warns_status_not_matching_folder(store, env, capsys):
    r = env / "arbol"
    write(r, "_done/2026-01-01_a_b.md", "---\ntipo: plan\nestado: en_progreso\nfecha: 2026-01-01\n---\n# a\n")
    write(r, "_wip/2026-01-02_a_c.md", "---\nestado: en_progreso\nfecha: 2026-01-02\n---\n# c\n")
    err = run(capsys, *IMPORT, str(r)).err
    assert "does not match _done/ — it goes to wip" in err
    assert "type '' → infra" in err
    assert "has no _wip" in run(capsys, *IMPORT, str(env), ok=False).err


def test_export_reproduces_the_import(store, arbol, env, capsys):
    run(capsys, *IMPORT, str(arbol))
    dest = env / "export"
    out = run(capsys, *EXPORT, str(dest)).out
    assert "4 written" in out
    for rel in ("_done/2026-01-05_api_BUG_timeout.md", "_wip/2026-03-01_api_cache.md",
                "_wip/2026-03-09_api_HANDOFF_seguir.md"):
        # the same doc, with wip-doc's Spanish front-matter in cairn's vocabulary
        meta, body = parse_front_matter((arbol / rel).read_text())
        assert (dest / rel).read_text() == dump_front_matter(vocab.meta(meta)) + body, rel
    meta, _ = parse_front_matter((dest / "_ref/api_limites.md").read_text())
    assert meta["origin"] == ["../_done/no_existe.md", "../_done/2026-01-05_api_BUG_timeout.md"]
    assert (dest / "_done/2026-01-05_api_BUG_timeout.md").stat().st_mtime == 1_700_000_000

    assert "0 written, 4 unchanged" in run(capsys, *EXPORT, str(dest)).out
    run(capsys, "close", "2026-03-01_api_cache", "--force")
    err = run(capsys, *EXPORT, str(dest), ok=False).err
    assert "already exist with other content" in err      # the handoff now points to _done/
    r = run(capsys, *EXPORT, str(dest), "--force")
    assert "_wip/2026-03-01_api_cache.md matches no exported doc" in r.err
    assert (dest / "_done/2026-03-01_api_cache.md").is_file()


# ──────────────────────────────────────────────────────────────── leftovers

def test_leftovers(store, env, capsys):
    root = env / "projects"
    run(capsys, "new", "api", "timeout", "--repo", "api")
    name = f"{HOY}_api_timeout"
    write(root, f"_wip/{name}.md", "ya importado\n")
    write(root, "_wip/2026-03-09_api_suelto.md", "escrito a mano\n")
    write(root, "_wip/README.md", "no es un doc\n")
    write(root, "_wip/scripts/api_prueba.md", "un archivo, no un doc\n")
    write(root, "api/_wip/nota.md", "de antes del árbol único\n")
    write(root, "api/node_modules/x/_wip/y.md", "no se mira\n")

    p = wip_doc.leftovers(store)
    assert any(f"_wip/{name}.md: the doc is already in cairn" in x for x in p)
    assert any("_wip/2026-03-09_api_suelto.md: a doc outside cairn" in x for x in p)
    assert any(x.startswith("api/_wip/ has 1 .md file(s) inside a repo") for x in p)
    assert len(p) == 3, p          # not the README, nor scripts/, nor node_modules

    out = run(capsys, "migrate", "wip-doc", "check", ok=False).out
    assert "a doc outside cairn" in out and "3 wip-doc leftover(s)" in out
    assert "no wip-doc leftovers" in run(capsys, "migrate", "wip-doc", "check", str(env / "nada")).out


def test_core_check_does_not_look_at_wip_doc(store, env, capsys):
    write(env / "projects", "_wip/2026-03-09_api_suelto.md", "escrito a mano\n")
    write(env / "projects", "api/_wip/nota.md", "de antes del árbol único\n")
    assert "[OK] all in order" in run(capsys, "check").out
    config.update(lambda raw: raw.setdefault("docs", {}).update(file_dirs="scripts"))
    with Store(config.load()) as s:
        assert tree.problems(s) == ["_wip/2026-03-09_api_suelto.md: a stray .md — docs go in cairn; "
                                    "files, in _wip/ + scripts/"]


def test_recognizes_the_wip_doc_hook():
    assert wip_doc.is_hook("/home/p/.claude/skills/wip-doc/driver.py handoff hook")
    assert wip_doc.is_hook("python3 /x/wip-doc/driver.py handoff hook")
    assert not wip_doc.is_hook("/x/wip-doc/driver.py handoff list")
    assert not wip_doc.is_hook("/x/otro/driver.py handoff hook")
    assert not wip_doc.is_hook("/opt/cairn/bin/cairn handoff hook")
