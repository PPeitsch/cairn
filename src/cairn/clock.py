"""cairn's clock: a single place for "now", which tests can pin through an environment variable."""

import os
from datetime import UTC, datetime


def now() -> datetime:
    """Now, timezone-aware. `CAIRN_NOW` (ISO 8601) pins it; without a timezone it is taken as UTC."""
    fixed = os.environ.get("CAIRN_NOW")
    if fixed:
        dt = datetime.fromisoformat(fixed)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return datetime.now(UTC)


def timestamp() -> str:
    """An event's timestamp: UTC, ISO 8601 with microseconds (sorts as text)."""
    return now().astimezone(UTC).isoformat(timespec="microseconds")


def today() -> str:
    """Today's local date (`YYYY-MM-DD`), the one stamped on docs."""
    if os.environ.get("CAIRN_NOW"):
        return now().date().isoformat()
    return datetime.now(UTC).astimezone().date().isoformat()
