"""The data vocabulary: logs written with the old (Spanish) names keep projecting the same, mixed
with events in the canonical (English) one, and hand input in Spanish is saved in English."""

import json
import sqlite3

from cairn import config, docs, edit, vocab
from cairn.projection import SCHEMA_VERSION
from cairn.store import Store, new_doc_id

HOY = "2026-03-10"


def _emit(store, op, doc_id, payload):
    with store.writer() as w:
        w.emit(op, doc_id, payload)


def test_aliases():
    assert vocab.meta({"tipo": "plan", "estado": "en_progreso", "nota": "x", "extra": 1}) == \
        {"type": "plan", "status": "in_progress", "note": "x", "extra": 1}
    # with both names, the canonical one wins
    assert vocab.meta({"estado": "resuelto", "status": "in_progress"}) == {"status": "in_progress"}
    assert vocab.payload({"unset": ["nota", "reabierto"], "link_add": [["origen", "a"], ["plan", "b"]]}) \
        == {"unset": ["note", "reopened"], "link_add": [["origin", "a"], ["plan", "b"]]}
    assert vocab.status("usada") == "taken" and vocab.status("raro") == "raro"


def _old_and_new_doc(store):
    """The same life written twice: once as an old node wrote it (Spanish), once as a new one."""
    old, new = new_doc_id(), new_doc_id()
    ref = new_doc_id()
    _emit(store, "create", ref, {"name": "api_ref", "meta": {"type": "ref"}, "body": "# ref\n"})
    for doc_id, name, es in ((old, "2026-03-01_api_viejo", True), (new, "2026-03-01_api_nuevo", False)):
        k = (lambda s, e: s) if es else (lambda s, e: e)
        _emit(store, "create", doc_id, {"name": name, "body": "# api\n", "meta": {
            k("tipo", "type"): "plan", "repo": "api", k("estado", "status"): k("en_progreso", "in_progress"),
            k("fecha", "date"): "2026-03-01", k("nota", "note"): "baja prioridad"}})
        _emit(store, "link", doc_id, {"link_add": [[k("referencias", "references"), ref]]})
        _emit(store, "close", doc_id, {"set": {k("estado", "status"): k("completado", "completed"),
                                               k("cerrado", "closed"): "2026-03-05"},
                                       "unset": [k("nota", "note")]})
    return old, new, ref


def test_a_mixed_log_projects_like_an_all_english_one(store):
    old, new, ref = _old_and_new_doc(store)
    # and on top of the old doc, a new node keeps going in English
    for doc_id in (old, new):
        _emit(store, "reopen", doc_id, {"set": {"status": "in_progress", "reopened": HOY, "note": "volvió"},
                                        "unset": ["closed"]})
    a, b = docs.get(store, old), docs.get(store, new)
    assert a.meta == b.meta == {"type": "plan", "repo": "api", "status": "in_progress",
                                "date": "2026-03-01", "reopened": HOY, "note": "volvió"}
    assert a.scope == b.scope == "wip" and a.links == b.links == [["references", ref]]
    row = store.projection.db.execute("SELECT type, status, date FROM docs WHERE id = ?", (old,)).fetchone()
    assert tuple(row) == ("plan", "in_progress", "2026-03-01")
    # the earlier revisions too
    revs = [json.loads(r[0]) for r in store.projection.db.execute(
        "SELECT meta FROM revisions WHERE doc_id = ? ORDER BY seq", (old,))]
    assert revs[2]["status"] == "completed" and "estado" not in revs[2]


def test_the_log_is_not_rewritten_and_rebuild_reaches_the_same(store):
    old, _, _ = _old_and_new_doc(store)
    before = docs.get(store, old)
    lines = [line for f in sorted(store.config.logs_dir.rglob("*.jsonl"))
             for line in f.read_text().splitlines()]
    assert any('"estado":"en_progreso"' in line for line in lines)      # still as it was written
    store.rebuild()
    after = docs.get(store, old)
    assert (after.meta, after.links, after.scope) == (before.meta, before.links, before.scope)
    assert docs.check(store) == []


def test_old_handoffs_and_links(store):
    plan, h = new_doc_id(), new_doc_id()
    _emit(store, "create", plan, {"name": f"{HOY}_api_plan", "body": "# p\n",
                                  "meta": {"tipo": "plan", "estado": "en_progreso"}})
    _emit(store, "handoff_new", h, {"name": f"{HOY}_api_HANDOFF_seguir", "body": "# h\n",
                                    "meta": {"tipo": "handoff", "estado": "pendiente", "fecha": HOY,
                                             "repo": "api", "para": ["web", "api"]},
                                    "links": [["plan", plan]]})
    assert [d.name for d in docs.pending_handoffs(store, "web")] == [f"{HOY}_api_HANDOFF_seguir"]
    r = docs.handoff_take(store, None, "web")
    assert (r.doc.status, r.doc.meta["taken"], r.doc.meta["to"]) == ("taken", HOY, ["web", "api"])
    assert docs.reopen(store, r.doc.name, "de nuevo").doc.status == "pending"
    # an old link removed with its old name
    a, b = new_doc_id(), new_doc_id()
    _emit(store, "create", a, {"name": "a_x", "meta": {"tipo": "ref"}, "body": "# a\n"})
    _emit(store, "create", b, {"name": "b_x", "meta": {"type": "ref"}, "body": "# b\n"})
    _emit(store, "link", a, {"link_add": [["origen", b]]})
    assert docs.get(store, a).links == [["origin", b]]
    _emit(store, "unlink", a, {"link_remove": [["origin", b]]})
    assert docs.get(store, a).links == []


def test_hand_input_in_spanish_is_saved_in_english(store):
    doc = docs.new(store, "api", "cache", "plan", "api").doc
    edit.apply(store, doc.id, set_={"nota": "ojo", "estado": "en_monitoreo"})
    text = docs.render(store, docs.get(store, doc.id)).replace("note: ojo", "nota: otra cosa")
    edit.apply(store, doc.id, text=text)
    edit.apply(store, doc.id, unset=["nota"])
    payloads = [json.loads(r[0]) for r in store.projection.db.execute(
        "SELECT payload FROM events WHERE doc_id = ? ORDER BY rowid", (doc.id,))]
    assert payloads[1]["set"] == {"note": "ojo", "status": "monitoring"}
    assert payloads[2]["set"] == {"note": "otra cosa"} and payloads[3]["unset"] == ["note"]
    assert "note" not in docs.get(store, doc.id).meta


def test_an_older_schema_is_dropped_and_rebuilt(cfg):
    with Store(cfg) as s:
        docs.new(s, "api", "cache", "plan", "api")
    # a projection as version 1 left it: other columns, same log
    db = sqlite3.connect(cfg.db_path)
    db.executescript("DROP TABLE docs; CREATE TABLE docs (id TEXT PRIMARY KEY, tipo TEXT, estado TEXT, "
                     "updated TEXT); UPDATE info SET value = '1' WHERE key = 'schema';")
    db.commit()
    db.close()
    with Store(config.load()) as s:
        cols = {r[1] for r in s.projection.db.execute("PRAGMA table_info(docs)")}
        assert {"type", "status", "date", "last_event"} <= cols and "tipo" not in cols
        assert [d.name for d in docs.all_docs(s)] == [f"{HOY}_api_cache"]
        assert s.projection.db.execute("SELECT value FROM info WHERE key = 'schema'").fetchone()[0] \
            == str(SCHEMA_VERSION)
