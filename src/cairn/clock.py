"""Reloj de cairn: un solo lugar para "ahora", fijable por variable de entorno en los tests."""

import os
from datetime import UTC, datetime


def now() -> datetime:
    """Ahora, con zona horaria. `CAIRN_NOW` (ISO 8601) lo fija; sin zona se toma como UTC."""
    fijo = os.environ.get("CAIRN_NOW")
    if fijo:
        dt = datetime.fromisoformat(fijo)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return datetime.now(UTC)


def timestamp() -> str:
    """Marca de tiempo de un evento: UTC, ISO 8601 con microsegundos (ordena como texto)."""
    return now().astimezone(UTC).isoformat(timespec="microseconds")


def today() -> str:
    """La fecha local de hoy (`YYYY-MM-DD`), la que se estampa en los docs."""
    if os.environ.get("CAIRN_NOW"):
        return now().date().isoformat()
    return datetime.now(UTC).astimezone().date().isoformat()
