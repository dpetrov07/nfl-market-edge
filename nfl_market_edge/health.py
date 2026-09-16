"""Tiny structured health output shared by Railway workers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def health_record(collector: str, status: str, **details) -> dict:
    return {
        "record_type": "collector_status",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "collector": collector,
        "status": status,
        "railway_service": os.getenv("RAILWAY_SERVICE_NAME"),
        **details,
    }


def emit_health(record: dict, path: Path | None = None) -> None:
    """Print one Railway-friendly JSON line and optionally replace health.json."""
    print(json.dumps(record, separators=(",", ":"), default=str), flush=True)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, indent=2, default=str) + "\n")
    os.replace(temporary, path)
