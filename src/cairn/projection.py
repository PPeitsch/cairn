"""The projection: SQLite + FTS5 built from the log, rebuildable at any time.

Tables:

- `events`: every known event, with its `status` in the doc's chain (`applied`, `divergent` if
  it ended up on a branch that is not the main one, `resolved` if it belongs to a branch a
  `resolve` already settled, `orphan` if its `prev` has not arrived).
- `docs`: the current state of each doc (front-matter in `meta`, body in `body`). A deleted doc
  (`deleted:` in its state, see `delete`) is not here, nor in `links` or `docs_fts`: it keeps
  only its `events` and `revisions`, from which `restore` brings it back.
- `links`: the links between docs (`origin`, `references`, `plan`), by id.
- `revisions`: the state after each event of the main chain (history, diff, show).
- `docs_fts`: FTS5 index over name, title and body.
- `log_pos`: up to which byte of each log segment has already been projected.

A doc's state is a deterministic function of the set of its events: every time one arrives, the
whole doc is projected again. So the order they arrived in does not matter, and a `rebuild` from
the log reaches the same result as the incremental projection.

The state always uses the canonical vocabulary: events written with the old (Spanish) names are
translated when they are folded (`vocab`), so the tables never see them.
"""

from __future__ import annotations

import json
import re
import sqlite3
from copy import deepcopy
from dataclasses import dataclass, field
from operator import attrgetter
from pathlib import Path

from cairn import __version__, vocab
from cairn.events import CREATE_OPS, Event

SCHEMA_VERSION = 2

# statuses that leave the doc closed (the `done` scope)
CLOSED_STATES = frozenset({"resolved", "completed", "discarded", "archived", "taken", "dropped"})
OPEN_STATES = frozenset({"in_progress", "monitoring", "proposed", "pending"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS log_pos (
    node TEXT NOT NULL, segment TEXT NOT NULL, pos INTEGER NOT NULL,
    PRIMARY KEY (node, segment)
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, node TEXT NOT NULL, ts TEXT NOT NULL, op TEXT NOT NULL,
    doc_id TEXT NOT NULL, prev TEXT, payload TEXT NOT NULL, sig TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS events_doc ON events (doc_id);
CREATE TABLE IF NOT EXISTS docs (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT, repo TEXT, status TEXT,
    scope TEXT NOT NULL, date TEXT, closed TEXT, title TEXT, meta TEXT NOT NULL,
    body TEXT NOT NULL, head TEXT NOT NULL, created TEXT NOT NULL, last_event TEXT NOT NULL,
    divergent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS docs_name ON docs (name);
CREATE TABLE IF NOT EXISTS links (
    src TEXT NOT NULL, kind TEXT NOT NULL, dst TEXT NOT NULL, PRIMARY KEY (src, kind, dst)
);
CREATE INDEX IF NOT EXISTS links_dst ON links (dst);
CREATE TABLE IF NOT EXISTS revisions (
    event_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, seq INTEGER NOT NULL, ts TEXT NOT NULL,
    node TEXT NOT NULL, op TEXT NOT NULL, name TEXT NOT NULL, meta TEXT NOT NULL,
    body TEXT NOT NULL, links TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS revisions_doc ON revisions (doc_id, seq);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5 (
    doc_id UNINDEXED, name, title, body, tokenize = 'unicode61 remove_diacritics 2'
);
"""
_TABLES = ("log_pos", "events", "docs", "links", "revisions", "docs_fts")


# ──────────────────────────────────────────────────────────────── reducer

@dataclass
class DocState:
    name: str
    meta: dict
    body: str
    links: list[list[str]] = field(default_factory=list)   # [[kind, doc_id], …]

    @property
    def type(self) -> str:
        return str(self.meta.get("type", ""))

    @property
    def status(self) -> str:
        return str(self.meta.get("status", ""))

    @property
    def scope(self) -> str:
        """`ref`, `done` or `wip`: what `list` and `search` filter by."""
        if self.type == "ref":
            return "ref"
        return "done" if self.status in CLOSED_STATES else "wip"

    @property
    def deleted(self) -> bool:
        return bool(self.meta.get("deleted"))

    @property
    def closed(self) -> str | None:
        # a handoff is not closed: it is taken or dropped, and that date acts as the closing date
        for k in ("closed", "taken", "dropped"):
            if self.meta.get(k):
                return str(self.meta[k])
        return None

    @property
    def title(self) -> str:
        m = re.search(r"^# (.+)$", self.body, re.MULTILINE)
        return m.group(1).strip() if m else self.name


def fold(state: DocState | None, ev: Event) -> DocState:
    """Applies an event to a doc's state. Creating events carry the whole doc; the rest carry
    the specific changes, in the same shape for every operation:

        body          new body (replaces it)
        name          new name
        set / unset   front-matter fields that change / that are removed
        link_add      [[kind, doc_id], …] links that are added
        link_remove   same, links that are removed

    The payload is read in the canonical vocabulary, whichever one it was written with.
    """
    p = vocab.payload(ev.payload)
    if ev.op in CREATE_OPS:
        new = DocState(name=p["name"], meta=dict(p.get("meta") or {}), body=p.get("body", ""),
                       links=[list(x) for x in p.get("links") or []])
        return new
    if state is None:
        raise ValueError(f"{ev.op} on a doc that does not exist")
    new = deepcopy(state)
    if "name" in p:
        new.name = p["name"]
    if "body" in p:
        new.body = p["body"]
    for k in p.get("unset") or []:
        new.meta.pop(k, None)
    new.meta.update(p.get("set") or {})
    for link in p.get("link_remove") or []:
        if list(link) in new.links:
            new.links.remove(list(link))
    for link in p.get("link_add") or []:
        if list(link) not in new.links:
            new.links.append(list(link))
    return new


def chain(events: list[Event]) -> tuple[list[Event], set[str], set[str]]:
    """A doc's main chain, and which events are left out of it.

    Root: the event without `prev` (if there are several, the oldest by `(ts, id)`). From there
    it follows each event's child; if an event has more than one child, the chain forked (two
    nodes wrote on the same head): it follows the oldest child's branch and the rest is left
    `divergent`, to be resolved by hand. Whatever does not hang from any root is `orphan`.
    """
    key = attrgetter("ts", "id")
    roots = sorted((e for e in events if e.prev is None), key=key)
    children: dict[str, list[Event]] = {}
    for e in events:
        if e.prev is not None:
            children.setdefault(e.prev, []).append(e)
    main: list[Event] = []
    if roots:
        cur = roots[0]
        while True:
            main.append(cur)
            kids = sorted(children.get(cur.id, []), key=key)
            if not kids:
                break
            cur = kids[0]
    on_main = {e.id for e in main}
    # everything reachable from some root that is not on the main chain is divergent
    reachable: set[str] = set()
    stack = [r.id for r in roots]
    while stack:
        i = stack.pop()
        if i in reachable:
            continue
        reachable.add(i)
        stack.extend(c.id for c in children.get(i, []))
    divergent = reachable - on_main
    orphan = {e.id for e in events} - reachable
    return main, divergent, orphan


# ──────────────────────────────────────────────────────────────── projection

def _release(version: str | None) -> tuple[int, ...]:
    """`"0.5.0"` as `(0, 5, 0)`; no version (a projection from before 0.5.0) sorts first."""
    try:
        return tuple(int(p) for p in version.split("."))
    except (AttributeError, ValueError):
        return ()


class Projection:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute(_SCHEMA.split(";")[0])          # `info`, to read the versions first
        info = {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM info")}
        # the projection is disposable: with another schema its tables are dropped (emptying
        # them is not enough: they keep the old columns) and it is rebuilt from the log
        stale = "schema" in info and int(info["schema"]) != SCHEMA_VERSION
        # an older cairn leaves out what it cannot read and moves `log_pos` past it, so the first
        # open after an upgrade rebuilds too. An older one still running (an MCP server started
        # before the upgrade) leaves it alone: it would only rebuild it back and forth.
        projected_by = _release(info.get("cairn"))
        stale = stale or ("schema" in info and projected_by < _release(__version__))
        if stale:
            with self.db:
                for t in _TABLES:
                    self.db.execute(f"DROP TABLE IF EXISTS {t}")
        self.db.executescript(_SCHEMA)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO info VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            if projected_by <= _release(__version__):
                self.db.execute("INSERT OR REPLACE INTO info VALUES ('cairn', ?)", (__version__,))

    def close(self) -> None:
        self.db.close()

    def reset(self) -> None:
        """Empties the projection (the log is not touched)."""
        with self.db:
            for t in _TABLES:
                self.db.execute(f"DELETE FROM {t}")
            self.db.execute("INSERT OR REPLACE INTO info VALUES ('schema', ?)", (str(SCHEMA_VERSION),))

    # -- log positions
    def positions(self) -> dict[tuple[str, str], int]:
        return {(r["node"], r["segment"]): r["pos"] for r in self.db.execute("SELECT * FROM log_pos")}

    def set_position(self, node: str, segment: str, pos: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO log_pos VALUES (?, ?, ?)", (node, segment, pos))

    # -- ingestion
    def ingest(self, events: list[Event]) -> set[str]:
        """Records new events (repeated ones are ignored). Returns the affected docs. It does not
        commit: the caller does, together with the log positions."""
        touched = set()
        for ev in events:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO events (id, node, ts, op, doc_id, prev, payload, sig) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ev.id, ev.node, ev.ts, ev.op, ev.doc_id, ev.prev,
                 json.dumps(ev.payload, ensure_ascii=False), ev.sig))
            if cur.rowcount:
                touched.add(ev.doc_id)
        return touched

    def doc_events(self, doc_id: str) -> list[Event]:
        rows = self.db.execute("SELECT * FROM events WHERE doc_id = ?", (doc_id,)).fetchall()
        return [_row_event(r) for r in rows]

    def reproject(self, doc_id: str) -> None:
        """Rebuilds the state of the doc from all its events."""
        events = self.doc_events(doc_id)
        main, divergent, orphan = chain(events)
        # a `resolve` on the main chain settles the branches it names
        resolved = {i for e in main if e.op == "resolve" for i in e.payload.get("resolves") or []}
        resolved &= divergent
        divergent -= resolved
        db = self.db
        for ids, status in ((divergent, "divergent"), (orphan, "orphan"), (resolved, "resolved")):
            db.executemany("UPDATE events SET status = ? WHERE id = ?", [(status, i) for i in ids])
        db.executemany("UPDATE events SET status = 'applied' WHERE id = ?", [(e.id,) for e in main])
        db.execute("DELETE FROM revisions WHERE doc_id = ?", (doc_id,))
        db.execute("DELETE FROM links WHERE src = ?", (doc_id,))
        db.execute("DELETE FROM docs_fts WHERE doc_id = ?", (doc_id,))
        db.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
        if not main:
            return
        state: DocState | None = None
        last_event = main[0].ts
        for seq, ev in enumerate(main):
            state = fold(state, ev)
            # an imported doc keeps when it was last touched in the source tree
            last_event = (ev.payload.get("updated") or ev.ts) if ev.op == "import" else ev.ts
            db.execute(
                "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ev.id, doc_id, seq, ev.ts, ev.node, ev.op, state.name,
                 json.dumps(state.meta, ensure_ascii=False), state.body,
                 json.dumps(state.links, ensure_ascii=False)))
        assert state is not None
        if state.deleted:
            return
        db.execute(
            "INSERT INTO docs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, state.name, state.type, str(state.meta.get("repo") or ""), state.status,
             state.scope, str(state.meta.get("date") or ""), state.closed, state.title,
             json.dumps(state.meta, ensure_ascii=False), state.body, main[-1].id,
             main[0].ts, last_event, 1 if divergent else 0))
        db.executemany("INSERT OR IGNORE INTO links VALUES (?, ?, ?)",
                       [(doc_id, kind, dst) for kind, dst in state.links])
        db.execute("INSERT INTO docs_fts (doc_id, name, title, body) VALUES (?, ?, ?, ?)",
                   (doc_id, state.name, state.title, state.body))

    # -- reading
    def head(self, doc_id: str) -> str | None:
        """The last event of the main chain; a deleted doc has it too (its last revision)."""
        row = self.db.execute("SELECT head FROM docs WHERE id = ?", (doc_id,)).fetchone()
        if row:
            return row["head"]
        row = self.db.execute("SELECT event_id FROM revisions WHERE doc_id = ? ORDER BY seq DESC "
                              "LIMIT 1", (doc_id,)).fetchone()
        return row["event_id"] if row else None

    def deleted(self) -> list[tuple[str, DocState, sqlite3.Row]]:
        """The deleted docs: (id, state, last revision), the most recently deleted first."""
        rows = self.db.execute(
            "SELECT r.* FROM revisions r JOIN (SELECT doc_id, max(seq) AS seq FROM revisions "
            "GROUP BY doc_id) m ON r.doc_id = m.doc_id AND r.seq = m.seq "
            "WHERE r.doc_id NOT IN (SELECT id FROM docs) ORDER BY r.ts DESC").fetchall()
        out = []
        for r in rows:
            state = DocState(name=r["name"], meta=json.loads(r["meta"]), body=r["body"],
                             links=json.loads(r["links"]))
            if state.deleted:
                out.append((r["doc_id"], state, r))
        return out

    def state(self, doc_id: str) -> DocState | None:
        row = self.db.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            return None
        links = [[r["kind"], r["dst"]] for r in self.db.execute(
            "SELECT kind, dst FROM links WHERE src = ? ORDER BY rowid", (doc_id,))]
        return _row_state(row, links)

    def doc_ids(self) -> list[str]:
        return [r["id"] for r in self.db.execute("SELECT id FROM docs ORDER BY id")]

    def count_events(self) -> int:
        return self.db.execute("SELECT count(*) FROM events").fetchone()[0]


def _row_event(r: sqlite3.Row) -> Event:
    return Event(node=r["node"], ts=r["ts"], op=r["op"], doc_id=r["doc_id"],
                 payload=json.loads(r["payload"]), prev=r["prev"], sig=r["sig"], id=r["id"])


def _row_state(r: sqlite3.Row, links: list[list[str]]) -> DocState:
    return DocState(name=r["name"], meta=json.loads(r["meta"]), body=r["body"], links=links)
