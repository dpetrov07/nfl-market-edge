"""Small common record contract for live sportsbook adapters."""

from __future__ import annotations


SCHEMA_VERSION = 1
REQUIRED_SELECTION_FIELDS = {
    "sportsbook",
    "received_at",
    "event_id",
    "game",
    "market_id",
    "selection_id",
    "prop_type",
    "player",
    "side",
    "line",
    "american_odds",
    "decimal_odds",
    "state",
}


def selection_state_record(
    *,
    sportsbook: str,
    session_id: str,
    received_at: str,
    source: str,
    change_type: str,
    event: dict,
    selection: dict,
    changed: list[str],
) -> dict:
    """Return the canonical append-only record emitted by sportsbook collectors."""
    clean = {key: value for key, value in selection.items() if not key.startswith("_")}
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "selection_state",
        "change_type": change_type,
        "received_at": received_at,
        "source": source,
        "sportsbook": sportsbook,
        "session_id": session_id,
        **event,
        **clean,
        "changed": changed,
    }


def validate_selection_state(record: dict) -> None:
    """Fail fast when a new adapter omits fields needed for later matching."""
    missing = sorted(key for key in REQUIRED_SELECTION_FIELDS if key not in record)
    if missing:
        raise ValueError(f"sportsbook record is missing: {', '.join(missing)}")
