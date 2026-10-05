"""El almacén de un nodo: el log de eventos y su proyección, siempre en sintonía.

Toda escritura pasa por `Store.writer()`: toma el lock, pone la proyección al día con el log,
deja armar los eventos (con su `prev` calculado contra el head actual de cada doc), los agrega
al log y los proyecta. Si el bloque falla, no se escribe nada.

La proyección se pone al día sola (`catch_up`) leyendo lo que el log tiene después de la última
posición proyectada; así un evento que llegó por sync, o uno que quedó escrito en el log pero no
proyectado por un corte, se recupera sin intervención.

Cada evento va firmado con la clave del nodo (ver `keys`), que se genera sola la primera vez. Al
proyectar se verifica la firma de todos, propios y ajenos: uno sin firma válida de un nodo
conocido no entra a la base (queda en `problems` y `check` lo muestra), llegue como llegue al log.
"""

from __future__ import annotations

import fcntl
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Self

from cairn import clock, keys
from cairn.config import Config
from cairn.eventlog import EventLog
from cairn.events import CREATE_OPS, Event
from cairn.projection import DocState, Projection


class CairnError(Exception):
    """Un error para mostrarle al usuario (sin traceback)."""


@dataclass
class Writer:
    store: Store
    events: list[Event] = field(default_factory=list)
    heads: dict[str, str] = field(default_factory=dict)

    def emit(self, op: str, doc_id: str, payload: dict) -> Event:
        prev = self.heads.get(doc_id) or self.store.projection.head(doc_id)
        ev = Event.new(node=self.store.config.node, ts=clock.timestamp(), op=op, doc_id=doc_id,
                       payload=payload, prev=None if op in CREATE_OPS else prev)
        if ev.prev is None and prev is not None:
            raise CairnError(f"{op}: el doc {doc_id} ya existe")
        ev = Event(**{**ev.__dict__, "sig": keys.sign(self.store.key, ev.id)})
        self.events.append(ev)
        self.heads[doc_id] = ev.id
        return ev


def new_doc_id() -> str:
    return uuid.uuid4().hex


class Verifier:
    """Qué nodos se conocen y con qué clave: la propia y las de los pares autorizados. Un par
    revocado conserva su clave para verificar lo que ya escribió (el sync rechaza lo nuevo)."""

    def __init__(self, config: Config, own: keys.Ed25519PublicKey):
        self.node = config.node
        self.keys = {config.node: own}
        self.bad_peers: list[str] = []
        for name, peer in config.peers.items():
            if name == config.node:
                continue
            try:
                self.keys[name] = keys.parse_public(peer.key)
            except keys.KeyFileError as e:
                self.bad_peers.append(f"[peers.{name}]: {e}")

    def problem(self, ev: Event) -> str | None:
        """Por qué no se acepta el evento, o None si la firma es válida."""
        pub = self.keys.get(ev.node)
        if pub is None:
            return f"evento {ev.id[:12]} de un nodo no autorizado ({ev.node}): `cairn authorize`"
        if not keys.verify(pub, ev.id, ev.sig):
            return f"evento {ev.id[:12]} de {ev.node} con firma inválida o sin firma"
        return None


class Store:
    def __init__(self, config: Config):
        self.config = config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = EventLog(config.logs_dir, config.node)
        self.projection = Projection(config.db_path)
        self.problems: list[str] = []      # líneas del log que no se proyectaron, y por qué
        with self._lock():
            self.key = self._node_key()
            self.verifier = Verifier(config, self.key.public_key())
            self.catch_up()

    def _node_key(self) -> keys.Ed25519PrivateKey:
        path = self.config.key_path
        if not path.exists():
            return keys.generate(path, self.config.node)
        try:
            return keys.load_private(path)
        except keys.KeyFileError as e:
            raise CairnError(str(e)) from None

    def public_line(self) -> str:
        return keys.public_line(self.key.public_key(), self.config.node)

    def close(self) -> None:
        self.projection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _lock(self):
        self.config.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config.lock_path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    # -- sincronía log → proyección
    def catch_up(self) -> int:
        """Proyecta lo que el log tiene y la proyección todavía no. Devuelve cuántos eventos."""
        pos = self.projection.positions()
        sizes = self.log.sizes()
        new: list[Event] = []
        ends: dict[tuple[str, str], int] = {}
        for key, size in sizes.items():
            start = pos.get(key, 0)
            if size <= start:
                continue
            for entry in self.log.read_segment(*key, start=start):
                why = entry.error if entry.event is None else self.verifier.problem(entry.event)
                if why:
                    self.problems.append(f"logs/{entry.node}/{entry.segment}.jsonl byte "
                                         f"{entry.offset}: {why}")
                else:
                    new.append(entry.event)
                ends[key] = entry.end
        if not ends:
            return 0
        db = self.projection.db
        with db:
            touched = self.projection.ingest(new)
            for doc_id in sorted(touched):
                self.projection.reproject(doc_id)
            for (node, seg), end in ends.items():
                self.projection.set_position(node, seg, end)
        return len(new)

    def rebuild(self) -> int:
        """Descarta la proyección y la rehace desde el log."""
        with self._lock():
            self.problems.clear()
            self.projection.reset()
            self.catch_up()
            return self.projection.count_events()

    @contextmanager
    def writer(self):
        with self._lock():
            self.catch_up()
            w = Writer(self)
            yield w
            if w.events:
                self.log.append(w.events)
                self.catch_up()

    @contextmanager
    def receiving(self):
        """Para el sync: bajo el lock se agregan al log los bloques que llegaron de otros nodos;
        al salir se proyectan."""
        with self._lock():
            yield self.log
            self.catch_up()

    # -- lectura
    def state(self, doc_id: str) -> DocState | None:
        return self.projection.state(doc_id)


def open_store(config: Config) -> Store:
    return Store(config)
