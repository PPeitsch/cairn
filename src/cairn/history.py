"""Historial de un doc: cada evento de su cadena deja una revisión (`log`, `show doc@rev`, `diff`).

Una revisión se nombra por el hash de su evento; alcanza un prefijo de 4+ caracteres, como en git.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass

from cairn import docs
from cairn.docs import LINK_KINDS, Doc
from cairn.events import Event
from cairn.projection import DocState, chain, fold
from cairn.store import CairnError, Store


@dataclass
class Rev:
    event_id: str
    seq: int
    ts: str
    node: str
    op: str
    doc: Doc            # el doc como quedó después de este evento


def revisions(store: Store, doc: Doc) -> list[Rev]:
    rows = store.projection.db.execute(
        "SELECT * FROM revisions WHERE doc_id = ? ORDER BY seq", (doc.id,)).fetchall()
    out = []
    for r in rows:
        meta, links = json.loads(r["meta"]), json.loads(r["links"])
        st = DocState(name=r["name"], meta=meta, body=r["body"], links=links)
        snap = Doc(id=doc.id, name=r["name"], meta=meta, body=r["body"], scope=st.scope,
                   head=r["event_id"], created=doc.created, updated=r["ts"], divergent=False, links=links)
        out.append(Rev(r["event_id"], r["seq"], r["ts"], r["node"], r["op"], snap))
    return out


def split_ref(ref: str) -> tuple[str, str | None]:
    """`doc@abc123` → (`doc`, `abc123`)."""
    if "@" in ref:
        name, rev = ref.rsplit("@", 1)
        return name, rev or None
    return ref, None


def pick(revs: list[Rev], rev: str) -> Rev:
    """Una revisión por prefijo de hash, o relativa al head: `~1` es la anterior."""
    if rev.startswith("~") and rev[1:].isdigit():
        i = len(revs) - 1 - int(rev[1:])
        if i < 0:
            raise CairnError(f"El doc tiene {len(revs)} revisión(es): no hay {rev}")
        return revs[i]
    if len(rev) < 4:
        raise CairnError("Una revisión se nombra con al menos 4 caracteres del hash (o ~N)")
    found = [r for r in revs if r.event_id.startswith(rev)]
    if not found:
        raise CairnError(f"No hay una revisión {rev} en este doc (`cairn log <doc>` las lista)")
    if len(found) > 1:
        raise CairnError(f"La revisión {rev} es ambigua: usá más caracteres")
    return found[0]


def show(store: Store, ref: str) -> tuple[Doc, str]:
    """(doc, texto) del doc, o de una revisión si viene `doc@rev`."""
    name, rev = split_ref(ref)
    doc = docs.find(store, name)
    if rev is None:
        return doc, docs.render(store, doc)
    snap = pick(revisions(store, doc), rev).doc
    return snap, docs.render(store, snap)


def diff(store: Store, ref: str, a: str | None = None, b: str | None = None) -> str:
    """Diff unificado entre dos revisiones. Sin revisiones: el último cambio; con una: desde ahí
    hasta el head."""
    doc = docs.find(store, ref)
    revs = revisions(store, doc)
    if a is None:
        if len(revs) < 2:
            return ""
        ra, rb = revs[-2], revs[-1]
    else:
        ra = pick(revs, a)
        rb = pick(revs, b) if b else revs[-1]
    ta, tb = docs.render(store, ra.doc), docs.render(store, rb.doc)
    return "".join(difflib.unified_diff(
        ta.splitlines(keepends=True), tb.splitlines(keepends=True),
        fromfile=f"{ra.doc.name}@{ra.event_id[:8]}", tofile=f"{rb.doc.name}@{rb.event_id[:8]}"))


@dataclass
class LogLine:
    event_id: str
    ts: str
    node: str
    op: str
    doc_name: str
    status: str


def log(store: Store, ref: str | None = None, limit: int | None = None) -> list[LogLine]:
    """Los eventos más nuevos primero: de un doc (con las ramas divergentes y los huérfanos, si
    hay) o de toda la base."""
    db = store.projection.db
    sql = ("SELECT e.id, e.ts, e.node, e.op, e.status, coalesce(d.name, e.doc_id) AS name "
           "FROM events e LEFT JOIN docs d ON d.id = e.doc_id "
           "LEFT JOIN revisions r ON r.event_id = e.id")
    params: tuple = ()
    if ref is not None:
        # el de un doc sigue su cadena (no el reloj de cada nodo); lo que quedó afuera, al final
        sql += " WHERE e.doc_id = ? ORDER BY (r.seq IS NULL), r.seq DESC, e.ts DESC, e.rowid DESC"
        params = (docs.find(store, ref).id,)
    else:
        sql += " ORDER BY e.ts DESC, e.rowid DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [LogLine(r["id"], r["ts"], r["node"], r["op"], r["name"], r["status"])
            for r in db.execute(sql, params)]


# ──────────────────────────────────────────────────────────────── ramas divergentes

@dataclass
class Branch:
    tip: Event                  # el último evento de la rama
    events: list[Event]         # los de la rama que no están en la principal, en orden
    state: DocState             # cómo queda el doc si se sigue esta rama


def branches(store: Store, doc: Doc) -> list[Branch]:
    """Las ramas sin resolver de un doc divergente: una por cada punta."""
    events = store.projection.doc_events(doc.id)
    by_id = {e.id: e for e in events}
    main, divergent, _ = chain(events)
    estados = dict(store.projection.db.execute(
        "SELECT id, status FROM events WHERE doc_id = ?", (doc.id,)).fetchall())
    pend = {i for i in divergent if estados.get(i) == "divergent"}
    parents = {by_id[i].prev for i in pend}
    out = []
    for tip_id in sorted(pend - parents, key=lambda i: (by_id[i].ts, i)):
        path, cur = [], by_id[tip_id]
        while cur is not None:
            path.append(cur)
            cur = by_id.get(cur.prev) if cur.prev else None
        path.reverse()
        state = None
        for e in path:
            state = fold(state, e)
        out.append(Branch(by_id[tip_id], [e for e in path if e.id in divergent], state))
    return out


def _change(cur: Doc, target: DocState) -> dict:
    """El payload que lleva el doc de su estado actual al de `target`."""
    p: dict = {}
    if target.body != cur.body:
        p["body"] = target.body
    if target.name != cur.name:
        p["name"] = target.name
    set_ = {k: v for k, v in target.meta.items() if cur.meta.get(k) != v}
    unset = sorted(k for k in cur.meta if k not in target.meta)
    if set_:
        p["set"] = set_
    if unset:
        p["unset"] = unset
    add = [x for x in target.links if x not in cur.links and x[0] in LINK_KINDS]
    rem = [x for x in cur.links if x not in target.links]
    if add:
        p["link_add"] = add
    if rem:
        p["link_remove"] = rem
    return p


def resolve(store: Store, ref: str, keep: str) -> tuple[Doc, int]:
    """Resuelve las ramas divergentes de un doc. `keep`: `main` (queda como está) o el hash (4+
    caracteres) de la punta de una rama, cuyo estado pasa a ser el del doc. Devuelve (doc,
    eventos resueltos)."""
    with store.writer() as w:
        doc = docs.find(store, ref)
        if not doc.divergent:
            raise CairnError(f"{doc.name} no tiene ramas divergentes.")
        ramas = branches(store, doc)
        resolves = sorted(e.id for b in ramas for e in b.events)
        payload: dict = {}
        if keep != "main":
            if len(keep) < 4:
                raise CairnError("La rama se nombra con al menos 4 caracteres del hash de su punta")
            found = [b for b in ramas if b.tip.id.startswith(keep)]
            if len(found) != 1:
                raise CairnError(f"{keep} no es la punta de una rama de {doc.name}"
                                 + (" (ambiguo)" if found else "") + ": `cairn resolve "
                                 f"{doc.name}` las lista")
            payload = _change(doc, found[0].state)
        w.emit("resolve", doc.id, {**payload, "resolves": resolves})
    return docs.get(store, doc.id), len(resolves)


def branch_diff(store: Store, doc: Doc, b: Branch) -> str:
    snap = Doc(id=doc.id, name=b.state.name, meta=b.state.meta, body=b.state.body, scope=b.state.scope,
               head=b.tip.id, created=doc.created, updated=b.tip.ts, divergent=True, links=b.state.links)
    ta, tb = docs.render(store, doc), docs.render(store, snap)
    return "".join(difflib.unified_diff(
        ta.splitlines(keepends=True), tb.splitlines(keepends=True),
        fromfile=f"{doc.name}@{doc.head[:8]} (principal)", tofile=f"{doc.name}@{b.tip.id[:8]} (rama)"))
