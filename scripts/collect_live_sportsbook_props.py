"""Continuously save current NFL player-prop odds from DraftKings and Bovada."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests


DK_LEAGUE_ID = 88808
DK_URL = (
    "https://sportsbook-nash.draftkings.com/api/sportscontent/dkusnj/v1/"
    "leagues/{league}/categories/{category}/subcategories/{subcategory}"
)
DK_PROPS = {
    "receiving_yards": (1342, 14114),
    "rushing_yards": (1001, 9514),
    "receptions": (1342, 14115),
}
BOVADA_URL = (
    "https://www.bovada.lv/services/sports/event/coupon/events/A/description/"
    "football/nfl?lang=en"
)
BOVADA_PROPS = {
    "Total Receiving Yards": "receiving_yards",
    "Total Rushing Yards": "rushing_yards",
    "Total Receptions": "receptions",
}
SCHEMA_VERSION = 1
STOP = False


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


def normalize_game(value: str) -> str:
    value = value.lower().replace("san francisco", "sf").replace("los angeles", "la")
    return re.sub(r"[^a-z0-9]", "", value)


def game_matches(actual: str, wanted: str) -> bool:
    actual_key, wanted_key = normalize_game(actual), normalize_game(wanted)
    return wanted_key in actual_key or actual_key in wanted_key


def iso_from_epoch_ms(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def split_teams(event: dict) -> tuple[dict, dict]:
    home, away = {}, {}
    for team in event.get("participants", []):
        if team.get("type") != "Team":
            continue
        if team.get("venueRole") == "Home":
            home = team
        elif team.get("venueRole") == "Away":
            away = team
    if not home and " @ " in event.get("name", ""):
        away_name, home_name = event["name"].split(" @ ", 1)
        away, home = {"name": away_name}, {"name": home_name}
    return away, home


def selection_player(selection: dict) -> dict:
    return next(
        (p for p in selection.get("participants", []) if p.get("type") == "Player"),
        {},
    )


def fetch_draftkings(game: str, poll_id: str) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    for prop_type, (category, subcategory) in DK_PROPS.items():
        fetched_at = utc_now()
        url = DK_URL.format(
            league=DK_LEAGUE_ID, category=category, subcategory=subcategory
        )
        try:
            response = requests.get(url, impersonate="chrome120", timeout=15)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            errors.append(f"{prop_type}: {type(exc).__name__}: {exc}")
            continue

        events = {str(e["id"]): e for e in payload.get("events", [])}
        selections: dict[str, list[dict]] = {}
        for selection in payload.get("selections", []):
            selections.setdefault(str(selection.get("marketId")), []).append(selection)

        for market in payload.get("markets", []):
            event = events.get(str(market.get("eventId")))
            if not event or not game_matches(event.get("name", ""), game):
                continue
            away, home = split_teams(event)
            by_threshold: dict[float, dict[str, dict]] = {}
            for selection in selections.get(str(market.get("id")), []):
                side = str(selection.get("outcomeType") or selection.get("label", "")).lower()
                if "over" in side:
                    side = "over"
                elif "under" in side:
                    side = "under"
                else:
                    continue
                threshold = parse_float(selection.get("points"))
                if threshold is not None:
                    by_threshold.setdefault(threshold, {})[side] = selection

            for threshold, sides in by_threshold.items():
                over, under = sides.get("over", {}), sides.get("under", {})
                reference = over or under
                player = selection_player(reference)
                rows.append(
                    {
                        "record_type": "quote",
                        "schema_version": SCHEMA_VERSION,
                        "poll_id": poll_id,
                        "sportsbook": "draftkings",
                        "fetched_at": fetched_at,
                        "source_update_at": None,
                        "game": event.get("name"),
                        "event_id": str(event.get("id")),
                        "scheduled_start": event.get("startEventDate"),
                        "event_status": event.get("status"),
                        "is_live": str(event.get("status", "")).upper()
                        in {"STARTED", "LIVE", "IN_PROGRESS"},
                        "away_team": away.get("name"),
                        "away_team_id": str(away.get("id")) if away.get("id") else None,
                        "home_team": home.get("name"),
                        "home_team_id": str(home.get("id")) if home.get("id") else None,
                        "player": player.get("name") or market.get("name"),
                        "player_id": str(player.get("id")) if player.get("id") else None,
                        "player_team": player.get("metadata", {}).get("teamAbbreviation"),
                        "prop_type": prop_type,
                        "market_id": str(market.get("id")),
                        "threshold": threshold,
                        "over_odds": parse_american(over.get("displayOdds", {}).get("american")),
                        "under_odds": parse_american(under.get("displayOdds", {}).get("american")),
                        "over_selection_id": str(over.get("id")) if over.get("id") else None,
                        "under_selection_id": str(under.get("id")) if under.get("id") else None,
                    }
                )
    return rows, errors


def bovada_teams(event: dict) -> tuple[dict, dict]:
    home, away = {}, {}
    for team in event.get("competitors", []):
        if team.get("home"):
            home = team
        else:
            away = team
    return away, home


def fetch_bovada(game: str, poll_id: str) -> tuple[list[dict], list[str]]:
    fetched_at = utc_now()
    response = requests.get(BOVADA_URL, impersonate="chrome120", timeout=15)
    response.raise_for_status()
    payload = response.json()
    events = payload[0].get("events", []) if isinstance(payload, list) and payload else []
    rows = []
    for event in events:
        if not game_matches(event.get("description", ""), game):
            continue
        away, home = bovada_teams(event)
        for group in event.get("displayGroups", []):
            for market in group.get("markets", []):
                if market.get("status") != "O":
                    continue
                description = market.get("description", "")
                base = description.split(" - ", 1)[0].strip()
                prop_type = BOVADA_PROPS.get(base)
                if not prop_type:
                    continue
                player_text = description.split(" - ", 1)[-1].strip()
                team_match = re.search(r"\s+\(([^)]+)\)\s*$", player_text)
                player_team = team_match.group(1) if team_match else None
                player = re.sub(r"\s+\([^)]+\)\s*$", "", player_text).strip()
                by_threshold: dict[float, dict[str, dict]] = {}
                for outcome in market.get("outcomes", []):
                    if outcome.get("status") != "O":
                        continue
                    side = str(outcome.get("type") or outcome.get("description", "")).lower()
                    if side in {"o", "over"} or "over" in side:
                        side = "over"
                    elif side in {"u", "under"} or "under" in side:
                        side = "under"
                    else:
                        continue
                    threshold = parse_float(outcome.get("price", {}).get("handicap"))
                    if threshold is not None:
                        by_threshold.setdefault(threshold, {})[side] = outcome

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
                            "player": player,
                            "player_id": str(reference.get("competitorId"))
                            if reference.get("competitorId")
                            else None,
                            "player_team": player_team,
                            "prop_type": prop_type,
                            "market_id": str(market.get("id")),
                            "threshold": threshold,
                            "over_odds": parse_american(over.get("price", {}).get("american")),
                            "under_odds": parse_american(under.get("price", {}).get("american")),
                            "over_selection_id": str(over.get("id")) if over.get("id") else None,
                            "under_selection_id": str(under.get("id")) if under.get("id") else None,
                        }
                    )
    return rows, []


def source_record(
    poll_id: str, sportsbook: str, started_at: str, rows: list[dict], errors: list[str]
) -> dict:
    status = "partial" if rows and errors else "error" if errors else "ok" if rows else "empty"
    return {
        "record_type": "source_poll",
        "schema_version": SCHEMA_VERSION,
        "poll_id": poll_id,
        "sportsbook": sportsbook,
        "poll_started_at": started_at,
        "fetched_at": utc_now(),
        "status": status,
        "quote_count": len(rows),
        "matched_game_count": len({row["event_id"] for row in rows}),
        "errors": errors,
    }


def append_records(output_dir: Path, records: list[dict]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).date().isoformat()
    path = output_dir / f"nfl_player_props_{day}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        handle.flush()
    return path


def parse_args() -> argparse.Namespace:
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    default_output = os.getenv("SPORTSBOOK_OUTPUT_DIR") or volume or "data/raw/sportsbook_live"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", default=os.getenv("SPORTSBOOK_GAME"), help="Example: SF 49ers @ LA Rams")
    parser.add_argument(
        "--books",
        default=os.getenv("SPORTSBOOK_BOOKS", "draftkings,bovada"),
        help="Comma-separated: draftkings,bovada",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.getenv("SPORTSBOOK_POLL_SECONDS", "30")),
        help="Seconds from one poll start to the next",
    )
    parser.add_argument("--output-dir", type=Path, default=Path(default_output))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-polls", type=int, help="Useful for a short smoke test")
    args = parser.parse_args()
    if not args.game:
        parser.error("set --game or SPORTSBOOK_GAME")
    args.books = [book.strip().lower() for book in args.books.split(",") if book.strip()]
    unknown = set(args.books) - {"draftkings", "bovada"}
    if unknown:
        parser.error(f"unsupported books: {', '.join(sorted(unknown))}")
    return args


def request_stop(_signum, _frame) -> None:
    global STOP
    STOP = True


def main() -> None:
    args = parse_args()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    fetchers = {"draftkings": fetch_draftkings, "bovada": fetch_bovada}
    print(
        f"Collecting {args.game!r} from {', '.join(args.books)} every {args.interval:g}s "
        f"into {args.output_dir}",
        flush=True,
    )
    poll_number = 0
    while not STOP:
        cycle_started = time.monotonic()
        poll_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}-{uuid.uuid4().hex[:8]}"
        poll_started_at = utc_now()
        records = []
        for book in args.books:
            try:
                rows, errors = fetchers[book](args.game, poll_id)
            except Exception as exc:
                rows, errors = [], [f"{type(exc).__name__}: {exc}"]
            records.extend(rows)
            records.append(source_record(poll_id, book, poll_started_at, rows, errors))
            status = records[-1]["status"]
            print(f"{records[-1]['fetched_at']} {book}: {len(rows)} quotes ({status})", flush=True)
        path = append_records(args.output_dir, records)
        print(f"saved {len(records)} records -> {path}", flush=True)
        poll_number += 1
        if args.once or (args.max_polls and poll_number >= args.max_polls):
            break
        remaining = args.interval - (time.monotonic() - cycle_started)
        if remaining > 0:
            time.sleep(remaining)


if __name__ == "__main__":
    main()
