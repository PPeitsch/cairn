"""A doc's history: each event in its chain leaves a revision (`log`, `show doc@rev`, `diff`).

A revision is named by its event's hash; a prefix of 4+ characters is enough, as in git.
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
    doc: Doc            # the doc as it was after this event


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
    """A revision by hash prefix, or relative to the head: `~1` is the previous one."""
    if rev.startswith("~") and rev[1:].isdigit():
        i = len(revs) - 1 - int(rev[1:])
        if i < 0:
            raise CairnError(f"The doc has {len(revs)} revision(s): there is no {rev}")
        return revs[i]
    if len(rev) < 4:
        raise CairnError("A revision is named with at least 4 characters of its hash (or ~N)")
    found = [r for r in revs if r.event_id.startswith(rev)]
    if not found:
        raise CairnError(f"There is no revision {rev} in this doc (`cairn log <doc>` lists them)")
    if len(found) > 1:
        raise CairnError(f"Revision {rev} is ambiguous: use more characters")
    return found[0]


def show(store: Store, ref: str) -> tuple[Doc, str]:
    """(doc, text) of the doc, or of a revision if given `doc@rev`."""
    name, rev = split_ref(ref)
    doc = docs.find(store, name)
    if rev is None:
        return doc, docs.render(store, doc)
    snap = pick(revisions(store, doc), rev).doc
    return snap, docs.render(store, snap)


def diff(store: Store, ref: str, a: str | None = None, b: str | None = None) -> str:
    """Unified diff between two revisions. With none: the last change; with one: from there to
    the head."""
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
    """The newest events first: of one doc (with its divergent branches and orphans, if any) or
    of the whole database."""
    db = store.projection.db
    sql = ("SELECT e.id, e.ts, e.node, e.op, e.status, coalesce(d.name, e.doc_id) AS name "
           "FROM events e LEFT JOIN docs d ON d.id = e.doc_id "
           "LEFT JOIN revisions r ON r.event_id = e.id")
    params: tuple = ()
    if ref is not None:
        # a doc's log follows its chain (not each node's clock); whatever is left out goes last
        sql += " WHERE e.doc_id = ? ORDER BY (r.seq IS NULL), r.seq DESC, e.ts DESC, e.rowid DESC"
        params = (docs.find(store, ref).id,)
    else:
        sql += " ORDER BY e.ts DESC, e.rowid DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [LogLine(r["id"], r["ts"], r["node"], r["op"], r["name"], r["status"])
            for r in db.execute(sql, params)]


# ──────────────────────────────────────────────────────────────── divergent branches

@dataclass
class Branch:
    tip: Event                  # the branch's last event
    events: list[Event]         # the branch's events that are not on the main chain, in order
    state: DocState             # what the doc looks like if this branch is followed


def branches(store: Store, doc: Doc) -> list[Branch]:
    """The unresolved branches of a divergent doc: one per tip."""
    events = store.projection.doc_events(doc.id)
    by_id = {e.id: e for e in events}
    main, divergent, _ = chain(events)
    statuses = dict(store.projection.db.execute(
        "SELECT id, status FROM events WHERE doc_id = ?", (doc.id,)).fetchall())
    pend = {i for i in divergent if statuses.get(i) == "divergent"}
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
    """The payload that takes the doc from its current state to `target`'s."""
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
    """Resolves a doc's divergent branches. `keep`: `main` (it stays as it is) or the hash (4+
    characters) of a branch tip, whose state becomes the doc's. Returns (doc, resolved
    events)."""
    with store.writer() as w:
        doc = docs.find(store, ref)
        if not doc.divergent:
            raise CairnError(f"{doc.name} has no divergent branches.")
        branch_list = branches(store, doc)
        resolves = sorted(e.id for b in branch_list for e in b.events)
        payload: dict = {}
        if keep != "main":
            if len(keep) < 4:
                raise CairnError("A branch is named with at least 4 characters of its tip's hash")
            found = [b for b in branch_list if b.tip.id.startswith(keep)]
            if len(found) != 1:
                raise CairnError(f"{keep} is not the tip of a branch of {doc.name}"
                                 + (" (ambiguous)" if found else "") + ": `cairn resolve "
                                 f"{doc.name}` lists them")
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
