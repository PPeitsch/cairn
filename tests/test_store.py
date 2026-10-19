import json

from cairn import config
from cairn.eventlog import EventLog
from cairn.events import Event
from cairn.store import Store, new_doc_id


def create(store, name="2026-03-10_x_tema", meta=None, body="# x — tema\n"):
    doc_id = new_doc_id()
    with store.writer() as w:
        w.emit("create", doc_id, {"name": name, "meta": meta or {"tipo": "plan", "estado": "en_progreso"},
                                  "body": body})
    return doc_id


def snapshot(store):
    db = store.projection.db
    return {t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
            for t in ("docs", "links", "revisions", "events")}


def test_create_and_change(store):
    doc = create(store)
    with store.writer() as w:
        w.emit("edit", doc, {"body": "# x — tema\n\nnuevo\n", "set": {"nota": "algo"}})
        w.emit("close", doc, {"set": {"estado": "completado", "cerrado": "2026-03-10"},
                              "unset": ["nota"]})
    st = store.state(doc)
    assert st.body.endswith("nuevo\n")
    assert st.meta == {"tipo": "plan", "estado": "completado", "cerrado": "2026-03-10"}
    assert st.scope == "done" and st.closed == "2026-03-10" and st.title == "x — tema"
    revs = store.projection.db.execute("SELECT op FROM revisions WHERE doc_id = ? ORDER BY seq",
                                       (doc,)).fetchall()
    assert [r[0] for r in revs] == ["create", "edit", "close"]


def test_prev_chains_events(store):
    doc = create(store)
    with store.writer() as w:
        a = w.emit("edit", doc, {"body": "a"})
        b = w.emit("edit", doc, {"body": "b"})
    assert b.prev == a.id
    assert store.projection.head(doc) == b.id


def test_failed_block_writes_nothing(store):
    doc = create(store)
    try:
        with store.writer() as w:
            w.emit("edit", doc, {"body": "a"})
            raise RuntimeError("corte")
    except RuntimeError:
        pass
    assert store.projection.count_events() == 1
    assert len(list(store.log.read_all())) == 1


def test_links(store):
    a, b = create(store, "a"), create(store, "b")
    with store.writer() as w:
        w.emit("link", a, {"link_add": [["referencias", b]]})
    assert store.state(a).links == [["referencias", b]]
    with store.writer() as w:
        w.emit("unlink", a, {"link_remove": [["referencias", b]]})
    assert store.state(a).links == []


def test_rebuild_matches_incremental(store):
    a = create(store, "a")
    b = create(store, "b")
    with store.writer() as w:
        w.emit("edit", a, {"body": "uno"})
        w.emit("link", b, {"link_add": [["origen", a]]})
        w.emit("close", a, {"set": {"estado": "completado"}})
    before = snapshot(store)
    store.rebuild()
    assert snapshot(store) == before


def test_projection_is_rebuilt_from_scratch(cfg):
    with Store(cfg) as s:
        create(s)
    cfg.db_path.unlink()
    with Store(cfg) as s:                 # no database: it rebuilds itself from the log
        assert len(s.projection.doc_ids()) == 1


def test_catch_up_picks_events_from_other_nodes(cfg, peer):
    vps = peer("vps")
    store = Store(config.load())
    doc = create(store)
    head = store.projection.head(doc)
    # an event written by another node (as sync would leave it)
    other = vps.sign(Event.new(node="vps", ts="2026-03-11T00:00:00.000000+00:00", op="edit", doc_id=doc,
                               payload={"body": "desde el vps"}, prev=head))
    EventLog(cfg.logs_dir, "vps").append([other])
    assert store.catch_up() == 1
    assert store.state(doc).body == "desde el vps"
    store.close()


def test_events_without_valid_signature_are_not_projected(cfg, peer):
    vps = peer("vps")
    store = Store(config.load())
    doc = create(store)
    head = store.projection.head(doc)
    unsigned = Event.new(node="vps", ts="2026-03-11T00:00:00.000000+00:00", op="edit", doc_id=doc,
                         payload={"body": "sin firma"}, prev=head)
    intruso = Event.new(node="intruso", ts="2026-03-11T00:00:01.000000+00:00", op="edit", doc_id=doc,
                        payload={"body": "intruso"}, prev=head)
    forged = Event(**{**unsigned.__dict__, "sig": vps.sign(intruso).sig})
    EventLog(cfg.logs_dir, "vps").append([unsigned, forged])
    EventLog(cfg.logs_dir, "intruso").append([vps.sign(intruso)])
    assert store.catch_up() == 0
    assert store.state(doc).body == "# x — tema\n"
    assert len(store.problems) == 3
    assert any("no autorizado (intruso)" in p for p in store.problems)
    assert sum("firma inválida" in p for p in store.problems) == 2
    from cairn import docs
    assert len([p for p in docs.check(store) if "firma" in p or "autorizado" in p]) == 3
    store.close()


def test_divergence_keeps_both_branches(cfg, peer):
    vps = peer("vps")
    store = Store(config.load())
    doc = create(store)
    head = store.projection.head(doc)
    with store.writer() as w:
        mine = w.emit("edit", doc, {"body": "laptop"})
    theirs = vps.sign(Event.new(node="vps", ts="2026-03-11T00:00:00.000000+00:00", op="edit",
                                doc_id=doc, payload={"body": "vps"}, prev=head))
    EventLog(cfg.logs_dir, "vps").append([theirs])
    store.catch_up()
    row = store.projection.db.execute("SELECT divergent, body FROM docs WHERE id = ?", (doc,)).fetchone()
    assert row["divergent"] == 1
    assert row["body"] == "laptop"        # the oldest branch is the main one
    status = dict(store.projection.db.execute("SELECT id, status FROM events").fetchall())
    assert status[mine.id] == "applied" and status[theirs.id] == "divergent"
    before = snapshot(store)
    store.rebuild()                       # the result does not depend on arrival order
    assert snapshot(store) == before
    store.close()


def test_orphan_until_its_parent_arrives(cfg, peer):
    vps = peer("vps")
    store = Store(config.load())
    doc = create(store)
    head = store.projection.head(doc)
    first = vps.sign(Event.new(node="vps", ts="2026-03-11T00:00:00.000000+00:00", op="edit", doc_id=doc,
                               payload={"body": "1"}, prev=head))
    second = vps.sign(Event.new(node="vps", ts="2026-03-11T00:00:01.000000+00:00", op="edit", doc_id=doc,
                                payload={"body": "2"}, prev=first.id))
    store.projection.ingest([second])
    store.projection.reproject(doc)
    status = dict(store.projection.db.execute("SELECT id, status FROM events").fetchall())
    assert status[second.id] == "orphan"
    EventLog(cfg.logs_dir, "vps").append([first, second])
    store.catch_up()
    assert store.state(doc).body == "2"
    store.close()


def test_corrupt_line_is_reported_and_skipped(store, cfg):
    create(store)
    seg = cfg.logs_dir / "laptop" / "000001.jsonl"
    line = json.loads(seg.read_text().splitlines()[0])
    line["payload"]["body"] = "alterado"
    with open(seg, "a") as f:
        f.write(json.dumps(line) + "\n")
    store.catch_up()
    assert any("no coincide" in p for p in store.problems)
    assert len(store.projection.doc_ids()) == 1


def test_fts_index(store):
    create(store, body="# x\n\nla causa era el rate limit del endpoint\n")
    rows = store.projection.db.execute(
        "SELECT name FROM docs_fts WHERE docs_fts MATCH 'endpoint'").fetchall()
    assert len(rows) == 1
