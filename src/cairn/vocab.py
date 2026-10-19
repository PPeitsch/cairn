"""The data vocabulary: front-matter fields, statuses and link kinds.

The canonical vocabulary is the English one, and it is what every new event is written with. Logs
written before it (cairn started out in Spanish) carry the Spanish names, and the log is signed and
never rewritten: those names are read as aliases, forever. Everything that turns events or hand
input into a doc's state goes through here (`projection.fold`, `edit`, `migrate wip-doc import`),
so the rest of the code only ever sees the canonical names.

Only the vocabulary is translated: values such as `repo:`, `note:` or a doc's body are content.
"""

from __future__ import annotations

# front-matter fields (also the keys of `meta`, `set` and `unset` in the events)
FIELD_ALIASES = {
    "tipo": "type",
    "para": "to",
    "estado": "status",
    "nota": "note",
    "fecha": "date",
    "cerrado": "closed",
    "usada": "taken",
    "descartada": "dropped",
    "reabierto": "reopened",
    "actualizado": "updated",
    "autor": "author",
    "origen": "origin",
    "referencias": "references",
}

# values of `status`
STATUS_ALIASES = {
    "en_progreso": "in_progress",
    "pendiente": "pending",
    "en_monitoreo": "monitoring",
    "propuesto": "proposed",
    "resuelto": "resolved",
    "completado": "completed",
    "descartado": "discarded",
    "archivado": "archived",
    "usada": "taken",
    "descartada": "dropped",
}

# link kinds (`links`, `link_add`, `link_remove`); `plan` is the same in both
LINK_ALIASES = {"origen": "origin", "referencias": "references"}


def field(key: str) -> str:
    return FIELD_ALIASES.get(key, key)


def status(value):
    return STATUS_ALIASES.get(value, value) if isinstance(value, str) else value


def meta(raw: dict) -> dict:
    """A front-matter (or a `set`) with canonical keys and status. If a doc carries a field under
    both names, the canonical one wins: it is the newer."""
    out: dict = {}
    for k, v in raw.items():
        if k in FIELD_ALIASES:
            out.setdefault(FIELD_ALIASES[k], v)
    for k, v in raw.items():
        if k not in FIELD_ALIASES:
            out[k] = v
    if "status" in out:
        out["status"] = status(out["status"])
    return out


def fields(keys) -> list[str]:
    return [field(k) for k in keys]


def links(raw) -> list[list[str]]:
    return [[LINK_ALIASES.get(kind, kind), dst] for kind, dst in raw]


def payload(p: dict) -> dict:
    """An event's payload with the vocabulary in canonical form (a copy: the event is signed)."""
    out = dict(p)
    if "meta" in out:
        out["meta"] = meta(out["meta"] or {})
    if "set" in out:
        out["set"] = meta(out["set"] or {})
    if "unset" in out:
        out["unset"] = fields(out["unset"] or [])
    for k in ("links", "link_add", "link_remove"):
        if k in out:
            out[k] = links(out[k] or [])
    return out
