"""El log de eventos: archivos JSONL por nodo y segmento, sólo de agregado.

    logs/<nodo>/<segmento>.jsonl      segmento: 000001, 000002, …

Cada nodo escribe únicamente en su carpeta; las de los otros nodos llegan por sync. Un segmento
se cierra al pasar `SEGMENT_BYTES` y se sigue en el siguiente, así el sync puede intercambiar
segmentos enteros y comparar tamaños para saber qué falta.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cairn.events import NODE_RE, Event, EventError

SEGMENT_BYTES = 1 << 20
_SEG_SUFFIX = ".jsonl"


@dataclass(frozen=True)
class Entry:
    """Un evento leído del log, con su posición."""

    node: str
    segment: str
    offset: int          # byte donde empieza la línea
    end: int             # byte siguiente al `\n` que la cierra
    event: Event | None  # None si la línea está corrupta (ver `error`)
    error: str | None = None


class EventLog:
    def __init__(self, root: Path, node: str, segment_bytes: int = SEGMENT_BYTES):
        if not NODE_RE.match(node):
            raise ValueError(f"nombre de nodo inválido: {node!r}")
        self.root = root
        self.node = node
        self.segment_bytes = segment_bytes

    # -- estructura
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

    # -- escritura (sólo en el nodo propio)
    def _current_segment(self) -> Path:
        segs = self.segments(self.node)
        if not segs:
            return self.segment_path(self.node, "000001")
        last = self.segment_path(self.node, segs[-1])
        if last.stat().st_size >= self.segment_bytes:
            return self.segment_path(self.node, f"{int(segs[-1]) + 1:06d}")
        return last

    def append(self, events: list[Event]) -> None:
        """Agrega los eventos al segmento actual del nodo propio, en una sola escritura, y hace
        fsync: cuando vuelve, los eventos están en disco."""
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
            # una escritura cortada a la mitad deja la última línea sin `\n`: se cierra antes de
            # seguir, así la línea rota queda sola (y `check` la reporta) en vez de pegarse a esta
            if f.tell() > 0:
                with open(path, "rb") as r:
                    r.seek(-1, os.SEEK_END)
                    if r.read(1) != b"\n":
                        data = b"\n" + data
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    # -- lectura
    def read_segment(self, node: str, segment: str, start: int = 0):
        """Las entradas completas (terminadas en `\\n`) desde el byte `start`."""
        path = self.segment_path(node, segment)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read()
        except FileNotFoundError:
            return
        pos = start
        for raw in data.split(b"\n")[:-1]:   # lo que sigue al último \n está incompleto
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
        """Tamaño de cada segmento: lo que hay en disco, para comparar con lo ya proyectado."""
        return {(n, s): self.segment_path(n, s).stat().st_size
                for n in self.nodes() for s in self.segments(n)}
