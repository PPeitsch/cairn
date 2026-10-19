"""A node's store: the event log and its projection, always in step.

Every write goes through `Store.writer()`: it takes the lock, brings the projection up to date
with the log, lets the caller build the events (with their `prev` computed against each doc's
current head), appends them to the log and projects them. If the block fails, nothing is written.
If it wrote something, it triggers a background sync (`sync.trigger`, debounced).

The projection catches up on its own (`catch_up`) by reading what the log has after the last
projected position; so an event that arrived by sync, or one that was written to the log but not
projected because of a crash, is recovered without intervention.

Every event is signed with the node key (see `keys`), which is generated on first use. When
projecting, every signature is verified, own and foreign: an event without a valid signature
from a known node does not enter the database (it stays in `problems` and `check` shows it), no
matter how it reached the log.
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
    """An error meant for the user (shown without a traceback)."""


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
    """Which nodes are known and with which keys: the own one and those of the authorized peers.
    A revoked peer keeps its key to verify what it already wrote (sync rejects anything new). A
    previous key (`[...old.<n>]`) verifies only the events from before it was replaced."""

    def __init__(self, config: Config, own: keys.Ed25519PublicKey):
        self.node = config.node
        # node → [(key, until)]: the current one with `until` None
        self.keys: dict[str, list[tuple[keys.Ed25519PublicKey, str | None]]] = {
            config.node: [(own, None)]}
        self.bad_peers: list[str] = []
        self._add_old(config.node, config.old_keys, "node")
        for name, peer in config.peers.items():
            if name == config.node:
                continue
            try:
                self.keys[name] = [(keys.parse_public(peer.key), None)]
            except keys.KeyFileError as e:
                self.bad_peers.append(f"[peers.{name}]: {e}")
                continue
            self._add_old(name, peer.old, f"peers.{name}")

    def _add_old(self, node: str, old, where: str) -> None:
        for k in old:
            try:
                self.keys[node].append((keys.parse_public(k.key), k.until))
            except keys.KeyFileError as e:
                self.bad_peers.append(f"[{where}.old]: {e}")

    def problem(self, ev: Event) -> str | None:
        """Why the event is not accepted, or None if the signature is valid."""
        cands = self.keys.get(ev.node)
        if cands is None:
            return f"evento {ev.id[:12]} de un nodo no autorizado ({ev.node}): `cairn authorize`"
        for pub, until in cands:
            if keys.verify(pub, ev.id, ev.sig):
                if until is not None and ev.ts > until:
                    return (f"evento {ev.id[:12]} de {ev.node} firmado con una clave que se "
                            f"reemplazó el {until[:10]}, con fecha posterior")
                return None
        return f"evento {ev.id[:12]} de {ev.node} con firma inválida o sin firma"


class Store:
    def __init__(self, config: Config):
        self.config = config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = EventLog(config.logs_dir, config.node)
        self.projection = Projection(config.db_path)
        self.problems: list[str] = []      # log lines that were not projected, and why
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

    # -- keeping log → projection in step
    def catch_up(self) -> int:
        """Projects what the log has and the projection does not yet. Returns how many events."""
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
        """Discards the projection and rebuilds it from the log."""
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
        if w.events:
            # every write goes out to the remotes, whether from the MCP or the CLI: whatever is
            # not pushed stays only on this node. Late import: sync depends on store.
            from cairn import sync
            sync.trigger(self.config)

    @contextmanager
    def receiving(self):
        """For sync: under the lock, the chunks that arrived from other nodes are appended to
        the log; on exit they are projected."""
        with self._lock():
            yield self.log
            self.catch_up()

    # -- reading
    def state(self, doc_id: str) -> DocState | None:
        return self.projection.state(doc_id)


def open_store(config: Config) -> Store:
    return Store(config)
