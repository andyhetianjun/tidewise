"""Runtime switches: offline mode and the clock."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

# The bundled sample was fetched around this time. Offline mode pretends it is
# "now" so that questions like "tomorrow" have data behind them.
FIXTURE_NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)

DATA_DIR = Path(__file__).parent / "data"


def is_offline() -> bool:
    return os.environ.get("TIDEWISE_OFFLINE", "").lower() in {"1", "true", "yes"}


def now() -> datetime:
    """Current time in UTC. TIDEWISE_NOW overrides it (ISO 8601)."""
    override = os.environ.get("TIDEWISE_NOW")
    if override:
        return datetime.fromisoformat(override).astimezone(UTC)
    return FIXTURE_NOW if is_offline() else datetime.now(UTC)


def cache_dir() -> Path:
    return Path(os.environ.get("TIDEWISE_CACHE_DIR", Path.home() / ".cache" / "tidewise"))
