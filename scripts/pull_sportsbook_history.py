"""Pull a small historical NFL player-prop sample from The Odds API.

Historical player props require a paid The Odds API plan. Set THE_ODDS_API_KEY
in the environment. Responses are checkpointed before the normalized Parquet is
rebuilt, so reruns do not spend credits on completed snapshots.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import pandas as pd
import requests


API = "https://api.the-odds-api.com/v4/historical/sports/americanfootball_nfl"
DEFAULT_GAMES = ["2025_13_LA_CAR", "2025_16_PIT_DET"]
TEAM_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens", "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys", "DEN": "Denver Broncos",
    "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars", "KC": "Kansas City Chiefs",
    "LA": "Los Angeles Rams", "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings", "NE": "New England Patriots",
    "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers", "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}
MARKET_TO_PROP = {
    "player_reception_yds": "receiving_yards",
    "player_reception_yds_alternate": "receiving_yards",
    "player_rush_yds": "rushing_yards",
    "player_rush_yds_alternate": "rushing_yards",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", nargs="+", default=DEFAULT_GAMES)
    parser.add_argument(
        "--kalshi",
        type=Path,
        default=Path("data/processed/kalshi_pbp_pilot_audited.parquet"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/processed/sportsbook_prop_history_sample.parquet"),
    )
    parser.add_argument(
        "--cache", type=Path, default=Path("data/raw/the_odds_api_sample")
    )
    parser.add_argument("--include-alternates", action="store_true")
    parser.add_argument("--start-minutes", type=int, default=-15)
    parser.add_argument("--end-minutes-after-game", type=int, default=5)
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--estimate-only", action="store_true")
    return parser.parse_args()


def iso(value: pd.Timestamp) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def api_get(path: str, key: str, **params: object) -> tuple[dict, dict[str, str]]:
    response = requests.get(
        f"{API}/{path}", params={"apiKey": key, **params}, timeout=30
    )
    if response.status_code == 401:
        raise SystemExit("The Odds API rejected THE_ODDS_API_KEY.")
    if response.status_code == 422:
        raise SystemExit(f"The Odds API cannot serve this request: {response.text[:300]}")
    if response.status_code == 429:
        raise SystemExit("The Odds API rate or usage quota was exhausted.")
    response.raise_for_status()
    usage = {
        name: response.headers.get(name, "")
        for name in ("x-requests-last", "x-requests-used", "x-requests-remaining")
    }
    return response.json(), usage


def event_for_game(game_id: str, kickoff: pd.Timestamp, key: str, cache: Path) -> dict:
    event_file = cache / game_id / "event.json"
    if event_file.exists():
        return json.loads(event_file.read_text())

    payload, usage = api_get("events", key, date=iso(kickoff - pd.Timedelta(minutes=15)))
    away, home = game_id.split("_")[-2:]
    expected = {TEAM_NAMES[away], TEAM_NAMES[home]}
    matches = [
        event for event in payload.get("data", [])
        if {event.get("away_team"), event.get("home_team")} == expected
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one Odds API event for {game_id}; found {len(matches)}")
    event_file.parent.mkdir(parents=True, exist_ok=True)
    event_file.write_text(json.dumps(matches[0], indent=2))
    print(f"{game_id}: event {matches[0]['id']} (discovery cost {usage['x-requests-last'] or '?'})")
    return matches[0]


def american_implied(price: float) -> float:
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def api_key() -> str | None:
    key = os.environ.get("THE_ODDS_API_KEY")
    if key or not Path(".env").exists():
        return key
    for line in Path(".env").read_text().splitlines():
        if line.strip().startswith("THE_ODDS_API_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return None


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def normalize_snapshot(game_id: str, requested: str, payload: dict) -> list[dict]:
    snapshot = payload.get("timestamp")
    event = payload.get("data") or {}
    rows = []
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") not in MARKET_TO_PROP:
                continue
            for outcome in market.get("outcomes", []):
                side = str(outcome.get("name", "")).lower()
                if side not in {"over", "under"}:
                    continue
                price = float(outcome["price"])
                player = outcome.get("description") or outcome.get("participant")
                if not player or outcome.get("point") is None:
                    continue
                rows.append(
                    {
                        "provider": "the_odds_api",
                        "game_id": game_id,
                        "sportsbook_event_id": event.get("id"),
                        "requested_at": requested,
                        "snapshot_timestamp": snapshot,
                        "commence_time": event.get("commence_time"),
                        "home_team": event.get("home_team"),
                        "away_team": event.get("away_team"),
                        "bookmaker_key": bookmaker.get("key"),
                        "bookmaker": bookmaker.get("title"),
                        "bookmaker_updated_at": bookmaker.get("last_update"),
                        "market_updated_at": market.get("last_update"),
                        "source_market": market.get("key"),
                        "prop_type": MARKET_TO_PROP[market["key"]],
                        "player": player,
                        "player_key": normalize_name(player),
                        "threshold": float(outcome["point"]),
                        "side": side,
                        "american_odds": price,
                        "implied_probability": american_implied(price),
                    }
                )
    return rows


def add_no_vig(rows: pd.DataFrame) -> pd.DataFrame:
    keys = [
        "game_id", "snapshot_timestamp", "bookmaker_key", "source_market",
        "player_key", "prop_type", "threshold",
    ]
    probabilities = rows.pivot_table(
        index=keys, columns="side", values="implied_probability", aggfunc="first"
    ).reset_index()
    complete = probabilities.over.notna() & probabilities.under.notna()
    probabilities.loc[complete, "over_fair_probability"] = (
        probabilities.loc[complete, "over"]
        / (probabilities.loc[complete, "over"] + probabilities.loc[complete, "under"])
    )
    probabilities["under_fair_probability"] = 1 - probabilities.over_fair_probability
    rows = rows.merge(
        probabilities[keys + ["over_fair_probability", "under_fair_probability"]],
        on=keys,
        how="left",
    )
    rows["book_fair_probability"] = rows.over_fair_probability.where(
        rows.side.eq("over"), rows.under_fair_probability
    )
    consensus_keys = [
        "game_id", "snapshot_timestamp", "player_key", "prop_type", "threshold", "side"
    ]
    consensus = rows.groupby(consensus_keys, dropna=False).agg(
        consensus_fair_probability=("book_fair_probability", "mean"),
        consensus_book_count=("book_fair_probability", "count"),
    ).reset_index()
    return rows.merge(consensus, on=consensus_keys, how="left")


def main() -> None:
    args = parse_args()
    kalshi = pd.read_parquet(args.kalshi)
    kalshi = kalshi[kalshi.game_id.isin(args.games)].copy()
    if kalshi.empty:
        raise SystemExit("None of the requested games exist in the audited pilot.")

    markets = ["player_reception_yds", "player_rush_yds"]
    if args.include_alternates:
        markets += ["player_reception_yds_alternate", "player_rush_yds_alternate"]
    windows = {}
    for game_id in args.games:
        game = kalshi[kalshi.game_id.eq(game_id)]
        if game.empty:
            continue
        kickoff = pd.to_datetime(game.kickoff.iloc[0], utc=True)
        end = pd.to_datetime(game.loc[game.during_actual_game, "timestamp"].max(), utc=True)
        end += pd.Timedelta(minutes=args.end_minutes_after_game)
        windows[game_id] = pd.date_range(
            kickoff + pd.Timedelta(minutes=args.start_minutes), end, freq="5min"
        )
    maximum_credits = sum(map(len, windows.values())) * len(markets) * 10 + len(windows)
    print(
        f"Plan: {len(windows)} games, {sum(map(len, windows.values()))} snapshots, "
        f"{len(markets)} market keys; at most {maximum_credits:,} credits if every "
        "market is returned (empty/missing markets cost less)."
    )
    if args.estimate_only:
        return

    key = api_key()
    if not key and not args.cache_only:
        raise SystemExit(
            "Set THE_ODDS_API_KEY to use paid historical access, or use --cache-only "
            "to rebuild from existing checkpoints."
        )
    new_calls = 0
    for game_id in args.games:
        game = kalshi[kalshi.game_id.eq(game_id)]
        if game.empty:
            print(f"{game_id}: absent from audited pilot; skipped")
            continue
        kickoff = pd.to_datetime(game.kickoff.iloc[0], utc=True)
        end = pd.to_datetime(game.loc[game.during_actual_game, "timestamp"].max(), utc=True)
        end += pd.Timedelta(minutes=args.end_minutes_after_game)
        game_cache = args.cache / game_id
        if args.cache_only:
            if not game_cache.exists():
                print(f"{game_id}: no cache")
            continue
        event = event_for_game(game_id, kickoff, key, args.cache)
        for requested_at in windows[game_id]:
            # The extra 59 seconds selects that five-minute snapshot rather than
            # the preceding one when provider snapshots have non-zero seconds.
            requested_at += pd.Timedelta(seconds=59)
            checkpoint = game_cache / f"{requested_at.strftime('%Y%m%dT%H%M%SZ')}.json"
            if checkpoint.exists():
                continue
            payload, usage = api_get(
                f"events/{event['id']}/odds",
                key,
                regions="us",
                markets=",".join(markets),
                oddsFormat="american",
                dateFormat="iso",
                date=iso(requested_at),
            )
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            checkpoint.write_text(json.dumps(payload))
            new_calls += 1
            print(
                f"{game_id} {payload.get('timestamp')}: "
                f"cost {usage['x-requests-last'] or '?'}; "
                f"remaining {usage['x-requests-remaining'] or '?'}"
            )

    normalized = []
    for game_id in args.games:
        for checkpoint in sorted((args.cache / game_id).glob("*.json")):
            if checkpoint.name == "event.json":
                continue
            payload = json.loads(checkpoint.read_text())
            requested = checkpoint.stem
            normalized.extend(normalize_snapshot(game_id, requested, payload))
    if not normalized:
        raise SystemExit("No cached sportsbook outcomes were found; no output written.")
    rows = add_no_vig(pd.DataFrame(normalized))
    time_columns = [
        "requested_at", "snapshot_timestamp", "commence_time",
        "bookmaker_updated_at", "market_updated_at",
    ]
    for column in time_columns:
        rows[column] = pd.to_datetime(rows[column], utc=True, errors="coerce")
    rows = rows.drop_duplicates(
        ["game_id", "snapshot_timestamp", "bookmaker_key", "source_market",
         "player_key", "threshold", "side"],
        keep="last",
    ).sort_values(["game_id", "snapshot_timestamp", "player", "threshold", "bookmaker_key", "side"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows.to_parquet(args.output, index=False)
    print(
        f"Saved {len(rows):,} bookmaker outcomes, {rows.snapshot_timestamp.nunique():,} "
        f"snapshots, {rows.bookmaker_key.nunique():,} books to {args.output} "
        f"({new_calls} new paid snapshot calls)."
    )


if __name__ == "__main__":
    main()
