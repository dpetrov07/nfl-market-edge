"""Continuously save current NFL/CFB player-prop odds from supported books."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests as http_requests
from curl_cffi import requests

from nfl_market_edge.health import emit_health, health_record
from nfl_market_edge.sportsbook import (
    SCHEMA_VERSION,
    selection_state_record,
    validate_selection_state,
)


BOVADA_URL = (
    "https://www.bovada.lv/services/sports/event/coupon/events/A/description/"
    "football/nfl?lang=en"
)
BOVADA_URLS = {
    "nfl": BOVADA_URL,
    "ncaaf": (
        "https://www.bovada.lv/services/sports/event/coupon/events/A/description/"
        "football/college-football?lang=en"
    ),
}
FANDUEL_BASE_URL = os.getenv(
    "FANDUEL_BASE_URL", "https://sbapi.nj.sportsbook.fanduel.com/api"
).rstrip("/")
FANDUEL_PAGE_URL = f"{FANDUEL_BASE_URL}/content-managed-page"
FANDUEL_EVENT_URL = f"{FANDUEL_BASE_URL}/event-page"
FANDUEL_API_KEY = os.getenv("FANDUEL_API_KEY", "FhMFpcPWXMeyZxOx")
BETRIVERS_OPERATOR = os.getenv("BETRIVERS_OPERATOR", "rsiusnj")
BETRIVERS_BASE_URL = (
    f"https://eu-offering-api.kambicdn.com/offering/v2018/{BETRIVERS_OPERATOR}"
)
BETRIVERS_PATHS = {"nfl": "american_football/nfl", "ncaaf": "american_football/ncaaf"}
BOVADA_PROPS = {
    "Total Receiving Yards": "receiving_yards",
    "Total Rushing Yards": "rushing_yards",
    "Total Receptions": "receptions",
    "Alternate Receiving Yards": "receiving_yards",
    "Alternate Rushing Yards": "rushing_yards",
    "Alternate Receptions": "receptions",
}
FANDUEL_PROPS = {
    "Receiving Yds": ("receiving_yards", False),
    "Alt Receiving Yds": ("receiving_yards", True),
    "Rushing Yds": ("rushing_yards", False),
    "Alt Rushing Yds": ("rushing_yards", True),
    "Total Receptions": ("receptions", False),
    "Alt Receptions": ("receptions", True),
    "Passing Yds": ("passing_yards", False),
    "Alt Passing Yds": ("passing_yards", True),
    "Passing TDs": ("passing_touchdowns", False),
    "Alt Passing TDs": ("passing_touchdowns", True),
}
BETRIVERS_PROPS = {
    "Receiving Yards": "receiving_yards",
    "Rushing Yards": "rushing_yards",
    "Receptions": "receptions",
    "Passing Yards": "passing_yards",
    "Touchdown Passes": "passing_touchdowns",
}
NFL_CITY_ALIASES = {
    "ari": "arizona",
    "atl": "atlanta",
    "bal": "baltimore",
    "buf": "buffalo",
    "car": "carolina",
    "chi": "chicago",
    "cin": "cincinnati",
    "cle": "cleveland",
    "dal": "dallas",
    "den": "denver",
    "det": "detroit",
    "gb": "green bay",
    "hou": "houston",
    "ind": "indianapolis",
    "jax": "jacksonville",
    "kc": "kansas city",
    "lv": "las vegas",
    "mia": "miami",
    "min": "minnesota",
    "ne": "new england",
    "no": "new orleans",
    "nyg": "new york giants",
    "nyj": "new york jets",
    "phi": "philadelphia",
    "pit": "pittsburgh",
    "sea": "seattle",
    "sf": "san francisco",
    "tb": "tampa bay",
    "ten": "tennessee",
    "was": "washington",
}
STOP = False
PERSISTED_SELECTION_FIELDS = (
    "line", "american_odds", "decimal_odds", "state", "event_status", "is_live"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_american(value) -> int | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text == "EVEN":
        return 100
    try:
        return int(text.replace("\u2212", "-"))
    except ValueError:
        return None


def parse_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def decimal_from_american(value: int | None) -> float | None:
    if value is None or value == 0:
        return None
    return 1 + (100 / abs(value) if value < 0 else value / 100)


def normalize_game(value: str) -> str:
    value = re.sub(r"\(\d+\)", "", value.lower()).replace("(fl)", " florida")
    for abbreviation, city in NFL_CITY_ALIASES.items():
        value = re.sub(rf"\b{abbreviation}\b", city, value)
    value = value.replace("los angeles", "la")
    return re.sub(r"[^a-z0-9]", "", value)


def game_matches(actual: str, wanted: str) -> bool:
    actual_key, wanted_key = normalize_game(actual), normalize_game(wanted)
    return wanted_key in actual_key or actual_key in wanted_key


def iso_from_epoch_ms(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def bovada_teams(event: dict) -> tuple[dict, dict]:
    home, away = {}, {}
    for team in event.get("competitors", []):
        if team.get("home"):
            home = team
        else:
            away = team
    return away, home


def bovada_prop_markets(event: dict, include_inactive: bool = False) -> list[dict]:
    """Flatten Bovada's player-prop markets without losing alternate lines."""
    found = []
    for group in event.get("displayGroups", []):
        for market in group.get("markets", []):
            if not include_inactive and market.get("status") != "O":
                continue
            description = market.get("description", "")
            base = description.split(" - ", 1)[0].strip()
            prop_type = BOVADA_PROPS.get(base)
            if not prop_type:
                continue
            is_alternate = base.startswith("Alternate ")
            player_text = description.split(" - ", 1)[-1].strip()
            team_match = re.search(r"\s+\(([^)]+)\)\s*$", player_text)
            outcomes = []
            for outcome in market.get("outcomes", []):
                if not include_inactive and outcome.get("status") != "O":
                    continue
                side = str(outcome.get("type") or outcome.get("description", "")).lower()
                if side in {"o", "over"} or "over" in side:
                    side = "over"
                elif side in {"u", "under"} or "under" in side:
                    side = "under"
                elif is_alternate:
                    side = "over"
                else:
                    continue
                threshold = parse_float(outcome.get("price", {}).get("handicap"))
                if threshold is None and is_alternate:
                    match = re.match(r"\s*(\d+(?:\.\d+)?)\+", outcome.get("description", ""))
                    if match:
                        threshold = float(match.group(1)) - 0.5
                if threshold is None:
                    continue
                outcomes.append(
                    {
                        "side": side,
                        "threshold": threshold,
                        "selection_id": str(outcome.get("id")),
                        "selection_status": outcome.get("status"),
                        "player_id": str(outcome.get("competitorId"))
                        if outcome.get("competitorId")
                        else None,
                        "american_odds": parse_american(outcome.get("price", {}).get("american")),
                        "decimal_odds": parse_float(outcome.get("price", {}).get("decimal")),
                    }
                )
            found.append(
                {
                    "market_id": str(market.get("id")),
                    "market_status": market.get("status"),
                    "market_description": description,
                    "prop_type": prop_type,
                    "is_alternate": is_alternate,
                    "player": re.sub(r"\s+\([^)]+\)\s*$", "", player_text).strip(),
                    "player_team": team_match.group(1) if team_match else None,
                    "outcomes": outcomes,
                }
            )
    return found


def fetch_bovada(game: str, poll_id: str, sport: str = "nfl") -> tuple[list[dict], list[str]]:
    fetched_at = utc_now()
    response = requests.get(BOVADA_URLS[sport], impersonate="chrome120", timeout=15)
    response.raise_for_status()
    payload = response.json()
    events = payload[0].get("events", []) if isinstance(payload, list) and payload else []
    rows = []
    for event in events:
        if not game_matches(event.get("description", ""), game):
            continue
        away, home = bovada_teams(event)
        prop_markets = {
            item["market_id"]: item for item in bovada_prop_markets(event)
        }
        for group in event.get("displayGroups", []):
            for market in group.get("markets", []):
                if market.get("status") != "O":
                    continue
                description = market.get("description", "")
                market_type = {"Moneyline": "moneyline", "Point Spread": "spread"}.get(description)
                if market_type and market.get("period", {}).get("main"):
                    sides = {
                        str(outcome.get("type", "")).lower(): outcome
                        for outcome in market.get("outcomes", [])
                        if outcome.get("status") == "O"
                    }
                    away_selection, home_selection = sides.get("a", {}), sides.get("h", {})
                    rows.append(
                        {
                            "record_type": "quote",
                            "schema_version": SCHEMA_VERSION,
                            "poll_id": poll_id,
                            "sportsbook": "bovada",
                            "fetched_at": fetched_at,
                            "source_update_at": iso_from_epoch_ms(event.get("lastModified")),
                            "game": event.get("description"),
                            "event_id": str(event.get("id")),
                            "scheduled_start": iso_from_epoch_ms(event.get("startTime")),
                            "event_status": event.get("status"),
                            "is_live": bool(event.get("live")),
                            "away_team": away.get("name"),
                            "away_team_id": str(away.get("id")) if away.get("id") else None,
                            "home_team": home.get("name"),
                            "home_team_id": str(home.get("id")) if home.get("id") else None,
                            "market_type": market_type,
                            "prop_type": None,
                            "market_id": str(market.get("id")),
                            "away_line": parse_float(away_selection.get("price", {}).get("handicap")),
                            "home_line": parse_float(home_selection.get("price", {}).get("handicap")),
                            "away_odds": parse_american(away_selection.get("price", {}).get("american")),
                            "home_odds": parse_american(home_selection.get("price", {}).get("american")),
                            "away_selection_id": str(away_selection.get("id"))
                            if away_selection.get("id")
                            else None,
                            "home_selection_id": str(home_selection.get("id"))
                            if home_selection.get("id")
                            else None,
                        }
                    )
                    continue
                prop_market = prop_markets.get(str(market.get("id")))
                if not prop_market:
                    continue
                by_threshold: dict[float, dict[str, dict]] = {}
                for outcome in prop_market["outcomes"]:
                    by_threshold.setdefault(outcome["threshold"], {})[
                        outcome["side"]
                    ] = outcome

                for threshold, sides in by_threshold.items():
                    over, under = sides.get("over", {}), sides.get("under", {})
                    reference = over or under
                    rows.append(
                        {
                            "record_type": "quote",
                            "schema_version": SCHEMA_VERSION,
                            "poll_id": poll_id,
                            "sportsbook": "bovada",
                            "fetched_at": fetched_at,
                            "source_update_at": iso_from_epoch_ms(event.get("lastModified")),
                            "game": event.get("description"),
                            "event_id": str(event.get("id")),
                            "scheduled_start": iso_from_epoch_ms(event.get("startTime")),
                            "event_status": event.get("status"),
                            "is_live": bool(event.get("live")),
                            "away_team": away.get("name"),
                            "away_team_id": str(away.get("id")) if away.get("id") else None,
                            "home_team": home.get("name"),
                            "home_team_id": str(home.get("id")) if home.get("id") else None,
                            "player": prop_market["player"],
                            "player_id": reference.get("player_id"),
                            "player_team": prop_market["player_team"],
                            "market_type": prop_market["prop_type"],
                            "prop_type": prop_market["prop_type"],
                            "market_id": str(market.get("id")),
                            "threshold": threshold,
                            "over_odds": over.get("american_odds"),
                            "under_odds": under.get("american_odds"),
                            "over_selection_id": over.get("selection_id"),
                            "under_selection_id": under.get("selection_id"),
                        }
                    )
    return rows, []


def fanduel_prop_market(market: dict) -> tuple[str, str, bool] | None:
    name = market.get("marketName", "")
    if " - " not in name:
        return None
    player, suffix = name.rsplit(" - ", 1)
    prop = FANDUEL_PROPS.get(suffix)
    if not prop:
        return None
    return player, prop[0], prop[1]


def fetch_fanduel(game: str, poll_id: str, sport: str = "nfl") -> tuple[list[dict], list[str]]:
    """Fetch FanDuel's unauthenticated frontend JSON; no cookies or browser needed."""
    response = http_requests.get(
        FANDUEL_PAGE_URL,
        params={"page": "CUSTOM", "customPageId": sport, "_ak": FANDUEL_API_KEY},
        headers={"Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    events = payload.get("attachments", {}).get("events", {})
    fetched_at = utc_now()
    rows = []
    errors = []
    for event_id, event in events.items():
        if " @ " not in event.get("name", "") or not game_matches(event.get("name", ""), game):
            continue
        try:
            detail_response = http_requests.get(
                FANDUEL_EVENT_URL,
                params={"eventId": event_id, "tab": "popular", "_ak": FANDUEL_API_KEY},
                headers={"Accept": "application/json"},
                timeout=20,
            )
            detail_response.raise_for_status()
            detail = detail_response.json()
        except Exception as exc:
            errors.append(f"event {event_id}: {type(exc).__name__}: {exc}")
            continue

        away, home = event["name"].split(" @ ", 1)
        markets = detail.get("attachments", {}).get("markets", {}).values()
        for market in markets:
            parsed = fanduel_prop_market(market)
            if not parsed or market.get("marketStatus") != "OPEN":
                continue
            player, prop_type, is_alternate = parsed
            by_threshold: dict[float, dict[str, dict]] = {}
            for runner in market.get("runners", []):
                if runner.get("runnerStatus") != "ACTIVE":
                    continue
                side = str(runner.get("result", {}).get("type", "")).lower()
                threshold = parse_float(runner.get("handicap"))
                if is_alternate:
                    match = re.search(r"(\d+(?:\.\d+)?)\+", runner.get("runnerName", ""))
                    if not match:
                        continue
                    side = "over"
                    threshold = float(match.group(1)) - 0.5
                elif side not in {"over", "under"}:
                    lowered = runner.get("runnerName", "").lower()
                    side = "over" if lowered.endswith(" over") else "under" if lowered.endswith(" under") else ""
                if side not in {"over", "under"} or threshold is None:
                    continue
                by_threshold.setdefault(threshold, {})[side] = runner

            for threshold, sides in by_threshold.items():
                over, under = sides.get("over", {}), sides.get("under", {})
                rows.append(
                    {
                        "record_type": "quote",
                        "schema_version": SCHEMA_VERSION,
                        "poll_id": poll_id,
                        "sportsbook": "fanduel",
                        "fetched_at": fetched_at,
                        "source_update_at": None,
                        "game": event.get("name"),
                        "event_id": str(event_id),
                        "scheduled_start": event.get("openDate"),
                        "event_status": "live" if event.get("inPlay") else "scheduled",
                        "is_live": bool(event.get("inPlay")),
                        "away_team": away,
                        "home_team": home,
                        "player": player,
                        "player_id": None,
                        "player_team": None,
                        "market_type": prop_type,
                        "prop_type": prop_type,
                        "is_alternate": is_alternate,
                        "market_id": str(market.get("marketId")),
                        "threshold": threshold,
                        "over_odds": parse_american(
                            over.get("winRunnerOdds", {}).get("americanDisplayOdds", {}).get("americanOdds")
                        ),
                        "under_odds": parse_american(
                            under.get("winRunnerOdds", {}).get("americanDisplayOdds", {}).get("americanOdds")
                        ),
                        "over_selection_id": str(over.get("selectionId")) if over else None,
                        "under_selection_id": str(under.get("selectionId")) if under else None,
                    }
                )
    return rows, errors


def betrivers_prop(criterion: str) -> tuple[str, bool, float | None] | None:
    for label, prop_type in BETRIVERS_PROPS.items():
        if criterion.startswith(f"Total {label}"):
            return prop_type, False, None
        match = re.match(rf"(\d+(?:\.\d+)?)\+ {re.escape(label)}\b", criterion, re.IGNORECASE)
        if match:
            return prop_type, True, float(match.group(1)) - 0.5
    return None


def fetch_betrivers(game: str, poll_id: str, sport: str = "nfl") -> tuple[list[dict], list[str]]:
    """Fetch BetRivers' public Kambi list and per-event JSON endpoints."""
    list_url = f"{BETRIVERS_BASE_URL}/listView/{BETRIVERS_PATHS[sport]}/all/all/matches.json"
    response = http_requests.get(list_url, params={"lang": "en_US", "market": "US"}, timeout=20)
    response.raise_for_status()
    wrappers = response.json().get("events", [])
    fetched_at = utc_now()
    rows = []
    errors = []
    for wrapper in wrappers:
        event = wrapper.get("event", {})
        event_id = event.get("id")
        if not event_id or not game_matches(event.get("name", ""), game):
            continue
        try:
            detail_response = http_requests.get(
                f"{BETRIVERS_BASE_URL}/betoffer/event/{event_id}.json",
                params={"lang": "en_US", "market": "US"},
                timeout=20,
            )
            detail_response.raise_for_status()
            offers = detail_response.json().get("betOffers", [])
        except Exception as exc:
            errors.append(f"event {event_id}: {type(exc).__name__}: {exc}")
            continue

        for offer in offers:
            if offer.get("betOfferType", {}).get("name") != "Player Occurrence Line":
                continue
            parsed = betrivers_prop(offer.get("criterion", {}).get("label", ""))
            if not parsed:
                continue
            prop_type, is_alternate, alternate_threshold = parsed
            grouped: dict[tuple[str, float], dict[str, dict]] = {}
            for outcome in offer.get("outcomes", []):
                if outcome.get("status") != "OPEN" or not outcome.get("participant"):
                    continue
                side = str(outcome.get("label", "")).lower()
                if side == "yes":
                    side = "over"
                if side not in {"over", "under"}:
                    continue
                line = alternate_threshold
                if line is None:
                    raw_line = parse_float(outcome.get("line"))
                    line = raw_line / 1000 if raw_line is not None else None
                if line is None:
                    continue
                grouped.setdefault((outcome["participant"], line), {})[side] = outcome

            for (player, threshold), sides in grouped.items():
                over, under = sides.get("over", {}), sides.get("under", {})
                rows.append(
                    {
                        "record_type": "quote",
                        "schema_version": SCHEMA_VERSION,
                        "poll_id": poll_id,
                        "sportsbook": "betrivers",
                        "fetched_at": fetched_at,
                        "source_update_at": None,
                        "game": event.get("name"),
                        "event_id": str(event_id),
                        "scheduled_start": event.get("start"),
                        "event_status": event.get("state"),
                        "is_live": event.get("state") != "NOT_STARTED",
                        "away_team": event.get("awayName"),
                        "home_team": event.get("homeName"),
                        "player": player,
                        "player_id": None,
                        "player_team": None,
                        "market_type": prop_type,
                        "prop_type": prop_type,
                        "is_alternate": is_alternate,
                        "market_id": str(offer.get("id")),
                        "threshold": threshold,
                        "over_odds": parse_american(over.get("oddsAmerican")),
                        "under_odds": parse_american(under.get("oddsAmerican")),
                        "over_selection_id": str(over.get("id")) if over else None,
                        "under_selection_id": str(under.get("id")) if under else None,
                    }
                )
    return rows, errors


def selection_records(
    rows: list[dict],
    session_id: str,
    slate_id: str,
    sport: str,
    snapshot_at: str | None = None,
) -> list[dict]:
    """Expand paired quote rows into the shared per-selection record contract."""
    records = []
    for row in rows:
        if not row.get("prop_type") or not row.get("player"):
            continue
        for side in ("over", "under"):
            selection_id = row.get(f"{side}_selection_id")
            odds = row.get(f"{side}_odds")
            if not selection_id or odds is None:
                continue
            record = selection_state_record(
                sportsbook=row["sportsbook"],
                session_id=session_id,
                received_at=row["fetched_at"],
                source="poll",
                change_type="snapshot",
                event={
                    "slate_id": slate_id,
                    "sport": sport,
                    "poll_id": row["poll_id"],
                    "snapshot_at": snapshot_at,
                    "source_update_at": row.get("source_update_at"),
                    "game": row["game"],
                    "event_id": row["event_id"],
                    "scheduled_start": row.get("scheduled_start"),
                    "event_status": row.get("event_status"),
                    "is_live": row.get("is_live"),
                    "away_team": row.get("away_team"),
                    "away_team_id": row.get("away_team_id"),
                    "home_team": row.get("home_team"),
                    "home_team_id": row.get("home_team_id"),
                },
                selection={
                    "player": row["player"],
                    "player_id": row.get("player_id"),
                    "player_team": row.get("player_team"),
                    "prop_type": row["prop_type"],
                    "is_alternate": bool(row.get("is_alternate")),
                    "side": side,
                    "line": row["threshold"],
                    "threshold": row["threshold"],
                    "market_id": row["market_id"],
                    "selection_id": selection_id,
                    "american_odds": odds,
                    "decimal_odds": decimal_from_american(odds),
                    "state": "open",
                },
                changed=["american_odds", "decimal_odds", "line", "state"],
            )
            validate_selection_state(record)
            records.append(record)
    return records


def source_record(
    *,
    poll_id: str,
    sportsbook: str,
    slate_id: str,
    sport: str,
    games: list[str],
    started_at: str,
    snapshot_at: str,
    records: list[dict],
    errors: list[str],
    elapsed_seconds: float,
    consecutive_failures: int,
    records_written: int | None = None,
) -> dict:
    status = (
        "partial"
        if records and errors
        else "error"
        if errors
        else "ok"
        if records
        else "empty"
    )
    return health_record(
        sportsbook,
        status,
        schema_version=SCHEMA_VERSION,
        poll_id=poll_id,
        sportsbook=sportsbook,
        slate_id=slate_id,
        sport=sport,
        requested_games=games,
        poll_started_at=started_at,
        snapshot_at=snapshot_at,
        elapsed_seconds=round(elapsed_seconds, 3),
        selection_count=len(records),
        records_written=records_written,
        matched_game_count=len({row["event_id"] for row in records}),
        consecutive_failures=consecutive_failures,
        errors=errors,
    )


def records_to_persist(
    records: list[dict],
    previous: dict[tuple, tuple],
    *,
    refresh: bool,
) -> list[dict]:
    """Persist price changes immediately and periodically re-confirm live prices."""
    output = []
    for record in records:
        key = (
            record["sportsbook"],
            record["event_id"],
            record["market_id"],
            record["selection_id"],
        )
        signature = tuple(record.get(field) for field in PERSISTED_SELECTION_FIELDS)
        prior = previous.get(key)
        if prior is None:
            changed = list(PERSISTED_SELECTION_FIELDS)
            record["change_type"] = "snapshot"
        elif signature != prior:
            changed = [
                field
                for field, old, new in zip(PERSISTED_SELECTION_FIELDS, prior, signature)
                if old != new
            ]
            record["change_type"] = "update"
        elif refresh:
            changed = []
            record["change_type"] = "refresh"
        else:
            previous[key] = signature
            continue
        record["changed"] = changed
        previous[key] = signature
        output.append(record)
    return output


def append_records(output_dir: Path, records: list[dict], sport: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).date().isoformat()
    path = output_dir / f"{sport}_props_{day}.jsonl.gz"
    with gzip.open(path, "at", encoding="utf-8", compresslevel=6) as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--book",
        choices=("bovada", "fanduel", "betrivers"),
        default=os.getenv("SPORTSBOOK_BOOK", "bovada").lower(),
    )
    parser.add_argument("--game", action="append", help="Repeat for each slate game")
    parser.add_argument(
        "--sport",
        choices=("nfl", "ncaaf"),
        default=os.getenv("SPORTSBOOK_SPORT", "nfl").lower(),
    )
    parser.add_argument(
        "--slate-id",
        default=os.getenv("SLATE_ID"),
        help="Stable join key shared with the Kalshi manifest",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.getenv("SPORTSBOOK_POLL_SECONDS", "30")),
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-polls", type=int)
    parser.add_argument(
        "--full-snapshot-seconds",
        type=float,
        default=float(os.getenv("SPORTSBOOK_FULL_SNAPSHOT_SECONDS", "90")),
        help="Re-confirm unchanged selections this often; changes are always immediate",
    )
    args = parser.parse_args()
    configured_games = os.getenv("SPORTSBOOK_GAMES") or os.getenv(
        "SPORTSBOOK_GAME", ""
    )
    args.games = args.game or [
        game.strip() for game in configured_games.split(",") if game.strip()
    ]
    if not args.games:
        parser.error("set --game or SPORTSBOOK_GAMES")
    if not args.slate_id:
        parser.error("set --slate-id or SLATE_ID")
    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.full_snapshot_seconds < args.interval:
        parser.error("--full-snapshot-seconds must be at least --interval")
    if args.output_dir is None:
        volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
        root = Path(
            os.getenv("COLLECTOR_OUTPUT_ROOT")
            or (str(Path(volume) / "combo_slates") if volume else "data/live/combo_slates")
        )
        args.output_dir = root / args.slate_id / "sportsbooks" / args.book
    return args


def request_stop(_signum, _frame) -> None:
    global STOP
    STOP = True


def main() -> None:
    args = parse_args()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    fetcher = {
        "bovada": fetch_bovada,
        "fanduel": fetch_fanduel,
        "betrivers": fetch_betrivers,
    }[args.book]
    session_id = (
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}-"
        f"{uuid.uuid4().hex[:8]}"
    )
    print(
        f"Collecting {args.sport} props for {len(args.games)} game(s) from "
        f"{args.book} every {args.interval:g}s into {args.output_dir}",
        flush=True,
    )
    emit_health(
        health_record(
            args.book,
            "starting",
            slate_id=args.slate_id,
            sport=args.sport,
            requested_games=args.games,
        ),
        args.output_dir / "health.json",
    )
    poll_number = consecutive_failures = 0
    selection_signatures: dict[tuple, tuple] = {}
    last_full_snapshot = 0.0
    while not STOP:
        cycle_started = time.monotonic()
        poll_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}-{uuid.uuid4().hex[:8]}"
        poll_started_at = utc_now()
        bucket_seconds = max(args.interval, 1)
        snapshot_at = datetime.fromtimestamp(
            (time.time() // bucket_seconds) * bucket_seconds,
            tz=timezone.utc,
        ).isoformat()
        raw_rows, errors = [], []
        for game in args.games:
            try:
                rows, game_errors = fetcher(game, poll_id, args.sport)
                raw_rows.extend(rows)
                errors.extend(f"{game}: {error}" for error in game_errors)
            except Exception as exc:
                errors.append(f"{game}: {type(exc).__name__}: {exc}")
        records = selection_records(
            raw_rows, session_id, args.slate_id, args.sport, snapshot_at
        )
        if not records and not errors:
            errors.append("no_matching_selections")
        consecutive_failures = consecutive_failures + 1 if not records else 0
        refresh = (
            not last_full_snapshot
            or time.monotonic() - last_full_snapshot >= args.full_snapshot_seconds
        )
        persisted = records_to_persist(
            records, selection_signatures, refresh=refresh
        )
        if records and refresh:
            last_full_snapshot = time.monotonic()
        status = source_record(
            poll_id=poll_id,
            sportsbook=args.book,
            slate_id=args.slate_id,
            sport=args.sport,
            games=args.games,
            started_at=poll_started_at,
            snapshot_at=snapshot_at,
            records=records,
            errors=errors,
            elapsed_seconds=time.monotonic() - cycle_started,
            consecutive_failures=consecutive_failures,
            records_written=len(persisted),
        )
        path = append_records(args.output_dir, [*persisted, status], args.sport)
        emit_health(status, args.output_dir / "health.json")
        print(
            f"available {len(records)} selections; saved {len(persisted)} -> {path}",
            flush=True,
        )
        poll_number += 1
        if args.once or (args.max_polls and poll_number >= args.max_polls):
            break
        if consecutive_failures:
            retry_delay = min(2 ** min(consecutive_failures, 6), 60)
            remaining = max(args.interval, retry_delay) - (
                time.monotonic() - cycle_started
            )
        else:
            remaining = args.interval - time.time() % args.interval
        if remaining > 0:
            time.sleep(remaining)


if __name__ == "__main__":
    main()
