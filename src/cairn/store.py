"""El almacén de un nodo: el log de eventos y su proyección, siempre en sintonía.

Toda escritura pasa por `Store.writer()`: toma el lock, pone la proyección al día con el log,
deja armar los eventos (con su `prev` calculado contra el head actual de cada doc), los agrega
al log y los proyecta. Si el bloque falla, no se escribe nada.

La proyección se pone al día sola (`catch_up`) leyendo lo que el log tiene después de la última
posición proyectada; así un evento que llegó por sync, o uno que quedó escrito en el log pero no
proyectado por un corte, se recupera sin intervención.
"""

from __future__ import annotations

import fcntl
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Self

from cairn import clock
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
        self.events.append(ev)
        self.heads[doc_id] = ev.id
        return ev


def new_doc_id() -> str:
    return uuid.uuid4().hex


class Store:
    def __init__(self, config: Config):
        self.config = config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = EventLog(config.logs_dir, config.node)
        self.projection = Projection(config.db_path)
        self.problems: list[str] = []      # líneas corruptas del log vistas al leerlo
        with self._lock():
            self.catch_up()

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
                if entry.event is None:
                    self.problems.append(f"logs/{entry.node}/{entry.segment}.jsonl byte "
                                         f"{entry.offset}: {entry.error}")
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

    # -- lectura
    def state(self, doc_id: str) -> DocState | None:
        return self.projection.state(doc_id)


def open_store(config: Config) -> Store:
    return Store(config)
