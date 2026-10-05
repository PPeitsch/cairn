"""Formato canónico de un evento y cómo se calcula su hash.

Un evento es una operación sobre un doc. Su `id` es el SHA-256 de la forma canónica: JSON UTF-8
con las claves ordenadas, sin espacios, de todos los campos salvo `id` y `sig`. La firma (`sig`,
ed25519 del nodo, fase 3) va sobre el `id`, así que no entra en el hash y se puede agregar o
verificar sin recalcularlo.

`prev` es el `id` del evento anterior del mismo doc (`None` en el que lo crea): forma una cadena
por doc, como los commits de git.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

FORMAT_VERSION = 1

# Operaciones que crean un doc (no tienen `prev`).
CREATE_OPS = frozenset({"create", "import", "handoff_new"})
# Operaciones que lo modifican. Todas se aplican igual (ver `projection.fold`); el nombre dice
# qué se hizo y es lo que muestran `log` y `weekly`.
CHANGE_OPS = frozenset({
    "edit", "set_state", "close", "reopen", "promote", "link", "unlink",
    "handoff_take", "handoff_drop",
})
OPS = CREATE_OPS | CHANGE_OPS

NODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class EventError(ValueError):
    """Un evento mal formado o cuyo hash no coincide con su contenido."""


def _check_json(value, where: str = "payload") -> None:
    """Sólo tipos con una única representación JSON: sin floats (su texto depende de la
    implementación y cambiaría el hash) ni claves que no sean strings."""
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        raise EventError(f"{where}: los floats no tienen forma canónica; usar str o int")
    if isinstance(value, list):
        for i, v in enumerate(value):
            _check_json(v, f"{where}[{i}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise EventError(f"{where}: clave no string {k!r}")
            _check_json(v, f"{where}.{k}")
        return
    raise EventError(f"{where}: tipo no serializable {type(value).__name__}")


def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class Event:
    node: str
    ts: str
    op: str
    doc_id: str
    payload: dict = field(default_factory=dict)
    prev: str | None = None
    v: int = FORMAT_VERSION
    sig: str | None = None
    id: str = ""

    # -- construcción
    @classmethod
    def new(cls, *, node: str, ts: str, op: str, doc_id: str, payload: dict,
            prev: str | None) -> Event:
        ev = cls(node=node, ts=ts, op=op, doc_id=doc_id, payload=payload, prev=prev)
        ev.validate(check_id=False)
        return ev.with_id()

    def hashed_fields(self) -> dict:
        return {"v": self.v, "node": self.node, "ts": self.ts, "op": self.op,
                "doc_id": self.doc_id, "payload": self.payload, "prev": self.prev}

    def compute_id(self) -> str:
        return hashlib.sha256(canonical_json(self.hashed_fields())).hexdigest()

    def with_id(self) -> Event:
        return Event(**{**self.__dict__, "id": self.compute_id()})

    # -- validación
    def validate(self, check_id: bool = True) -> None:
        if self.v != FORMAT_VERSION:
            raise EventError(f"versión de formato desconocida: {self.v}")
        if not NODE_RE.match(self.node or ""):
            raise EventError(f"nombre de nodo inválido: {self.node!r}")
        if self.op not in OPS:
            raise EventError(f"operación desconocida: {self.op!r}")
        if not isinstance(self.doc_id, str) or not self.doc_id:
            raise EventError("falta doc_id")
        if not isinstance(self.ts, str) or not self.ts:
            raise EventError("falta ts")
        if not isinstance(self.payload, dict):
            raise EventError("payload tiene que ser un objeto")
        _check_json(self.payload)
        if self.op in CREATE_OPS and self.prev is not None:
            raise EventError(f"{self.op} crea el doc: no lleva prev")
        if self.op in CHANGE_OPS and not (isinstance(self.prev, str) and _HASH_RE.match(self.prev)):
            raise EventError(f"{self.op} necesita prev (el hash del evento anterior del doc)")
        if self.sig is not None and not isinstance(self.sig, str):
            raise EventError("sig tiene que ser string")
        if check_id and self.id != self.compute_id():
            raise EventError(f"el id {self.id[:12]} no coincide con el contenido del evento")

    # -- serialización (una línea del log)
    def to_json(self) -> str:
        d = {**self.hashed_fields(), "id": self.id, "sig": self.sig}
        return canonical_json(d).decode("utf-8")

    @classmethod
    def from_json(cls, line: str) -> Event:
        try:
            d = json.loads(line)
        except ValueError as e:
            raise EventError(f"línea que no es JSON: {e}") from None
        if not isinstance(d, dict):
            raise EventError("el evento no es un objeto")
        known = {"v", "node", "ts", "op", "doc_id", "payload", "prev", "sig", "id"}
        extra = set(d) - known
        if extra:
            raise EventError(f"campos desconocidos: {', '.join(sorted(extra))}")
        try:
            ev = cls(**d)
        except TypeError as e:
            raise EventError(str(e)) from None
        ev.validate()
        return ev
