"""The event log: append-only JSONL files per node and segment.

    logs/<node>/<segment>.jsonl      segment: 000001, 000002, …

Each node writes only to its own folder; the other nodes' folders arrive by sync. A segment is
closed once it passes `SEGMENT_BYTES` and writing continues in the next one, so sync can exchange
whole segments and compare sizes to find out what is missing.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from cairn.events import NODE_RE, Event, EventError

SEGMENT_BYTES = 1 << 20
_SEG_SUFFIX = ".jsonl"


@dataclass(frozen=True)
class Entry:
    """An event read from the log, with its position."""

    node: str
    segment: str
    offset: int          # byte where the line starts
    end: int             # byte right after the `\n` that ends it
    event: Event | None  # None if the line is corrupt (see `error`)
    error: str | None = None


class EventLog:
    def __init__(self, root: Path, node: str, segment_bytes: int = SEGMENT_BYTES):
        if not NODE_RE.match(node):
            raise ValueError(f"nombre de nodo inválido: {node!r}")
        self.root = root
        self.node = node
        self.segment_bytes = segment_bytes

    # -- layout
    def nodes(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and NODE_RE.match(p.name))

    def segments(self, node: str) -> list[str]:
        d = self.root / node
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob(f"*{_SEG_SUFFIX}") if p.stem.isdigit())

    def segment_path(self, node: str, segment: str) -> Path:
        return self.root / node / f"{segment}{_SEG_SUFFIX}"

    # -- writing (own node only)
    def _current_segment(self) -> Path:
        segs = self.segments(self.node)
        if not segs:
            return self.segment_path(self.node, "000001")
        last = self.segment_path(self.node, segs[-1])
        if last.stat().st_size >= self.segment_bytes:
            return self.segment_path(self.node, f"{int(segs[-1]) + 1:06d}")
        return last

    def append(self, events: list[Event]) -> None:
        """Appends the events to the own node's current segment in a single write, and fsyncs:
        when it returns, the events are on disk."""
        if not events:
            return
        for ev in events:
            if ev.node != self.node:
                raise EventError(f"el nodo {self.node} no escribe eventos de {ev.node}")
            ev.validate()
        path = self._current_segment()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = "".join(ev.to_json() + "\n" for ev in events).encode("utf-8")
        with open(path, "ab") as f:
            # a write cut halfway leaves the last line without `\n`: close it before continuing, so
            # the broken line stays on its own (and `check` reports it) instead of merging with this one
            if f.tell() > 0:
                with open(path, "rb") as r:
                    r.seek(-1, os.SEEK_END)
                    if r.read(1) != b"\n":
                        data = b"\n" + data
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    # -- reading
    def read_segment(self, node: str, segment: str, start: int = 0):
        """The complete entries (ending in `\\n`) from byte `start` on."""
        path = self.segment_path(node, segment)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read()
        except FileNotFoundError:
            return
        pos = start
        for raw in data.split(b"\n")[:-1]:   # whatever follows the last \n is incomplete
            end = pos + len(raw) + 1
            if raw.strip():
                try:
                    ev = Event.from_json(raw.decode("utf-8"))
                    if ev.node != node:
                        raise EventError(f"evento de {ev.node} en el log de {node}")
                    yield Entry(node, segment, pos, end, ev)
                except (EventError, UnicodeDecodeError) as e:
                    yield Entry(node, segment, pos, end, None, str(e))
            pos = end

    def read_all(self):
        for node in self.nodes():
            for seg in self.segments(node):
                yield from self.read_segment(node, seg)

    def sizes(self) -> dict[tuple[str, str], int]:
        """Size of each segment: what is on disk, to compare with what was already projected."""
        return {(n, s): self.segment_path(n, s).stat().st_size
                for n in self.nodes() for s in self.segments(n)}

    # -- sync: segments as bytes
    def read_bytes(self, node: str, segment: str) -> bytes:
        try:
            return self.segment_path(node, segment).read_bytes()
        except FileNotFoundError:
            return b""

    def inventory(self) -> dict[str, dict[str, list]]:
        """`{node: {segment: [size, sha256]}}` of what is on disk up to the last `\\n` (a
        half-written line does not count)."""
        out: dict[str, dict[str, list]] = {}
        for n in self.nodes():
            for seg in self.segments(n):
                data = self.read_bytes(n, seg)
                data = data[:data.rfind(b"\n") + 1]
                out.setdefault(n, {})[seg] = [len(data), hashlib.sha256(data).hexdigest()]
        return out

    def append_raw(self, node: str, segment: str, offset: int, data: bytes, own: bool = False) -> None:
        """Appends bytes that arrived by sync to another node's segment (or to the own one, with
        `own`: the restore of a node that lost its data). Only if the local copy is exactly what
        the other side believed it was (`offset`): otherwise nothing is touched."""
        if node == self.node and not own:
            raise EventError(f"el log de {node} sólo lo escribe {node}")
        if not NODE_RE.match(node) or not (segment.isdigit() and len(segment) == 6):
            raise EventError(f"segmento inválido: {node}/{segment}")
        if not data.endswith(b"\n"):
            raise EventError("un bloque tiene que terminar en una línea completa")
        path = self.segment_path(node, segment)
        size = path.stat().st_size if path.exists() else 0
        if size != offset:
            raise EventError(f"{node}/{segment}: se esperaba continuar desde el byte {offset} y "
                             f"el log local tiene {size}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
