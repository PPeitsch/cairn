"""The canonical form of an event and how its hash is computed.

An event is an operation on a doc. Its `id` is the SHA-256 of the canonical form: UTF-8 JSON with
sorted keys and no whitespace, of every field except `id` and `sig`. The signature (`sig`, the
node's ed25519) covers the `id`, so it is not part of the hash and can be added or verified
without recomputing it.

`prev` is the `id` of the previous event of the same doc (`None` in the one that creates it): it
forms one chain per doc, like git commits.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

FORMAT_VERSION = 1

# Operations that create a doc (they have no `prev`).
CREATE_OPS = frozenset({"create", "import", "handoff_new"})
# Operations that modify it. They are all applied the same way (see `projection.fold`); the name
# says what was done and is what `log` and `weekly` show.
CHANGE_OPS = frozenset({
    "edit", "set_state", "close", "reopen", "promote", "link", "unlink",
    "handoff_take", "handoff_drop", "resolve", "rename", "delete", "restore",
})
OPS = CREATE_OPS | CHANGE_OPS

NODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class EventError(ValueError):
    """A malformed event, or one whose hash does not match its content."""


def _check_json(value, where: str = "payload") -> None:
    """Only types with a single JSON representation: no floats (their text depends on the
    implementation and would change the hash) and no non-string keys."""
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        raise EventError(f"{where}: floats have no canonical form; use str or int")
    if isinstance(value, list):
        for i, v in enumerate(value):
            _check_json(v, f"{where}[{i}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise EventError(f"{where}: non-string key {k!r}")
            _check_json(v, f"{where}.{k}")
        return
    raise EventError(f"{where}: non-serializable type {type(value).__name__}")


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

    # -- construction
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

    # -- validation
    def validate(self, check_id: bool = True) -> None:
        if self.v != FORMAT_VERSION:
            raise EventError(f"unknown format version: {self.v}")
        if not NODE_RE.match(self.node or ""):
            raise EventError(f"invalid node name: {self.node!r}")
        if self.op not in OPS:
            raise EventError(f"unknown operation: {self.op!r}")
        if not isinstance(self.doc_id, str) or not self.doc_id:
            raise EventError("missing doc_id")
        if not isinstance(self.ts, str) or not self.ts:
            raise EventError("missing ts")
        if not isinstance(self.payload, dict):
            raise EventError("payload has to be an object")
        _check_json(self.payload)
        if self.op in CREATE_OPS and self.prev is not None:
            raise EventError(f"{self.op} creates the doc: it has no prev")
        if self.op in CHANGE_OPS and not (isinstance(self.prev, str) and _HASH_RE.match(self.prev)):
            raise EventError(f"{self.op} needs prev (the hash of the doc's previous event)")
        if self.sig is not None and not isinstance(self.sig, str):
            raise EventError("sig has to be a string")
        if check_id and self.id != self.compute_id():
            raise EventError(f"the id {self.id[:12]} does not match the event's content")

    # -- serialization (one log line)
    def to_json(self) -> str:
        d = {**self.hashed_fields(), "id": self.id, "sig": self.sig}
        return canonical_json(d).decode("utf-8")

    @classmethod
    def from_json(cls, line: str) -> Event:
        try:
            d = json.loads(line)
        except ValueError as e:
            raise EventError(f"line that is not JSON: {e}") from None
        if not isinstance(d, dict):
            raise EventError("the event is not an object")
        known = {"v", "node", "ts", "op", "doc_id", "payload", "prev", "sig", "id"}
        extra = set(d) - known
        if extra:
            raise EventError(f"unknown fields: {', '.join(sorted(extra))}")
        try:
            ev = cls(**d)
        except TypeError as e:
            raise EventError(str(e)) from None
        ev.validate()
        return ev
