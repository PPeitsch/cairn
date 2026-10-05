"""La proyección: SQLite + FTS5 armada a partir del log, reconstruible en cualquier momento.

Tablas:

- `events`: todos los eventos conocidos, con su `status` en la cadena del doc (`applied`,
  `divergent` si quedó en una rama que no es la principal, `resolved` si es de una rama que un
  `resolve` ya decidió, `orphan` si su `prev` no llegó).
- `docs`: el estado actual de cada doc (front-matter en `meta`, cuerpo en `body`).
- `links`: los vínculos entre docs (`origen`, `referencias`, `plan`), por id.
- `revisions`: el estado después de cada evento de la cadena principal (historial, diff, show).
- `docs_fts`: índice FTS5 sobre nombre, título y cuerpo.
- `log_pos`: hasta qué byte de cada segmento del log ya se proyectó.

El estado de un doc es una función determinística del conjunto de sus eventos: cada vez que
llega uno, el doc se vuelve a proyectar entero. Así da lo mismo en qué orden llegaron, y una
`rebuild` desde el log llega al mismo resultado que la proyección incremental.
"""

from __future__ import annotations

import json
import re
import sqlite3
from copy import deepcopy
from dataclasses import dataclass, field
from operator import attrgetter
from pathlib import Path

from cairn.events import CREATE_OPS, Event

SCHEMA_VERSION = 1

# estados que dejan el doc cerrado (en el árbol de wip-doc: `_done/`)
CERRADOS = frozenset({"resuelto", "completado", "archivado", "usada", "descartada"})
ABIERTOS = frozenset({"en_progreso", "en_monitoreo", "propuesto", "pendiente"})

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
    id TEXT PRIMARY KEY, name TEXT NOT NULL, tipo TEXT, repo TEXT, estado TEXT,
    scope TEXT NOT NULL, fecha TEXT, closed TEXT, title TEXT, meta TEXT NOT NULL,
    body TEXT NOT NULL, head TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
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


# ──────────────────────────────────────────────────────────────── reducer

@dataclass
class DocState:
    name: str
    meta: dict
    body: str
    links: list[list[str]] = field(default_factory=list)   # [[kind, doc_id], …]

    @property
    def tipo(self) -> str:
        return str(self.meta.get("tipo", ""))

    @property
    def estado(self) -> str:
        return str(self.meta.get("estado", ""))

    @property
    def scope(self) -> str:
        """Dónde estaría en el árbol de wip-doc: `ref`, `done` o `wip`."""
        if self.tipo == "ref":
            return "ref"
        return "done" if self.estado in CERRADOS else "wip"

    @property
    def closed(self) -> str | None:
        # un handoff no se cierra: se usa o se descarta, y esa fecha hace de cierre
        for k in ("cerrado", "usada", "descartada"):
            if self.meta.get(k):
                return str(self.meta[k])
        return None

    @property
    def title(self) -> str:
        m = re.search(r"^# (.+)$", self.body, re.MULTILINE)
        return m.group(1).strip() if m else self.name


def fold(state: DocState | None, ev: Event) -> DocState:
    """Aplica un evento al estado de un doc. Los que crean traen el doc entero; el resto trae
    los cambios concretos, con la misma forma para todas las operaciones:

        body          cuerpo nuevo (reemplaza)
        name          nombre nuevo
        set / unset   campos del front-matter que cambian / que se sacan
        link_add      [[kind, doc_id], …] vínculos que se agregan
        link_remove   ídem, que se sacan
    """
    p = ev.payload
    if ev.op in CREATE_OPS:
        new = DocState(name=p["name"], meta=dict(p.get("meta") or {}), body=p.get("body", ""),
                       links=[list(x) for x in p.get("links") or []])
        return new
    if state is None:
        raise ValueError(f"{ev.op} sobre un doc que no existe")
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
    """La cadena principal de un doc y qué eventos quedan afuera.

    Raíz: el evento sin `prev` (si hay varias, la más vieja por `(ts, id)`). Desde ahí se sigue
    el hijo de cada evento; si un evento tiene más de un hijo, la cadena se bifurcó (dos nodos
    escribieron sobre el mismo head): sigue la rama del hijo más viejo y el resto queda
    `divergent`, para resolver a mano. Lo que no cuelga de ninguna raíz es `orphan`.
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
    # todo lo alcanzable desde alguna raíz que no está en la cadena principal es divergente
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


# ──────────────────────────────────────────────────────────────── proyección

class Projection:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(_SCHEMA)
        row = self.db.execute("SELECT value FROM info WHERE key = 'schema'").fetchone()
        if row is None:
            with self.db:
                self.db.execute("INSERT INTO info VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        elif int(row["value"]) != SCHEMA_VERSION:
            # la proyección es descartable: con otro esquema se rehace desde el log
            self.reset()

    def close(self) -> None:
        self.db.close()

    def reset(self) -> None:
        """Vacía la proyección (el log no se toca)."""
        with self.db:
            for t in ("log_pos", "events", "docs", "links", "revisions", "docs_fts"):
                self.db.execute(f"DELETE FROM {t}")
            self.db.execute("INSERT OR REPLACE INTO info VALUES ('schema', ?)", (str(SCHEMA_VERSION),))

    # -- posiciones del log
    def positions(self) -> dict[tuple[str, str], int]:
        return {(r["node"], r["segment"]): r["pos"] for r in self.db.execute("SELECT * FROM log_pos")}

    def set_position(self, node: str, segment: str, pos: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO log_pos VALUES (?, ?, ?)", (node, segment, pos))

    # -- ingesta
    def ingest(self, events: list[Event]) -> set[str]:
        """Registra eventos nuevos (los repetidos se ignoran). Devuelve los docs afectados.
        No hace commit: lo hace quien llama, junto con las posiciones del log."""
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
        """Rehace el estado del doc desde todos sus eventos."""
        events = self.doc_events(doc_id)
        main, divergent, orphan = chain(events)
        # un `resolve` en la cadena principal da por resueltas las ramas que nombra
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
        updated = main[0].ts
        for seq, ev in enumerate(main):
            state = fold(state, ev)
            # un doc importado conserva cuándo se tocó por última vez en el árbol de origen
            updated = (ev.payload.get("updated") or ev.ts) if ev.op == "import" else ev.ts
            db.execute(
                "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ev.id, doc_id, seq, ev.ts, ev.node, ev.op, state.name,
                 json.dumps(state.meta, ensure_ascii=False), state.body,
                 json.dumps(state.links, ensure_ascii=False)))
        assert state is not None
        db.execute(
            "INSERT INTO docs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, state.name, state.tipo, str(state.meta.get("repo") or ""), state.estado,
             state.scope, str(state.meta.get("fecha") or ""), state.closed, state.title,
             json.dumps(state.meta, ensure_ascii=False), state.body, main[-1].id,
             main[0].ts, updated, 1 if divergent else 0))
        db.executemany("INSERT OR IGNORE INTO links VALUES (?, ?, ?)",
                       [(doc_id, kind, dst) for kind, dst in state.links])
        db.execute("INSERT INTO docs_fts (doc_id, name, title, body) VALUES (?, ?, ?, ?)",
                   (doc_id, state.name, state.title, state.body))

    # -- lectura
    def head(self, doc_id: str) -> str | None:
        row = self.db.execute("SELECT head FROM docs WHERE id = ?", (doc_id,)).fetchone()
        return row["head"] if row else None

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
