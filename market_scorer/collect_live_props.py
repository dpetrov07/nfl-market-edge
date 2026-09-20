"""Collect low-frequency live Kalshi props for the side scorer."""

from __future__ import annotations

import argparse
import gzip
import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests


BASE = "https://external-api.kalshi.com/trade-api/v2"
ET = ZoneInfo("America/New_York")
SERIES = {
    "receiving_yards": "KXNFLRECYDS",
    "rushing_yards": "KXNFLRSHYDS",
}
TEAM_CODES = sorted(
    {
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL",
        "DEN", "DET", "GB", "HOU", "IND", "JAC", "JAX", "KC", "LA",
        "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG", "NYJ",
        "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS", "WSH",
    },
    key=len,
    reverse=True,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--today", action="store_true", help="All games dated today ET")
    target.add_argument("--game", help="Away/home abbreviations, e.g. NE_SEA")
    parser.add_argument("--interval", type=float, default=60, help="Seconds between snapshots")
    parser.add_argument("--once", action="store_true", help="Collect one snapshot and exit")
    parser.add_argument("--continue-after-kickoff", action="store_true")
    parser.add_argument(
        "--kickoff",
        help="Authoritative scheduled kickoff (UTC or offset timestamp). Overrides Kalshi occurrence_datetime.",
    )
    parser.add_argument(
        "--stop-hours-after-kickoff",
        type=float,
        default=5.0,
        help="Safety cutoff for --continue-after-kickoff (default: 5 hours)",
    )
    parser.add_argument("--display-players", type=int, default=6)
    parser.add_argument("--out", type=Path, default=Path("data/live"))
    return parser.parse_args()


def request_json(session: requests.Session, path: str, params=None) -> dict:
    last_error = None
    for attempt in range(4):
        try:
            response = session.get(f"{BASE}{path}", params=params, timeout=30)
        except requests.RequestException as exc:
            last_error = exc
            wait = min(2 ** attempt, 8)
            print(f"Kalshi connection error; retrying in {wait}s: {exc}", flush=True)
            time.sleep(wait)
            continue
        if response.status_code == 429:
            wait = min(2 ** attempt, 8)
            print(f"Kalshi rate limit; waiting {wait}s", flush=True)
            time.sleep(wait)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Kalshi request failed for {path}: {last_error or 'rate limit'}")


def paged_markets(session: requests.Session, **params) -> list[dict]:
    rows, cursor = [], None
    while True:
        query = {"limit": 1000, **params}
        if cursor:
            query["cursor"] = cursor
        payload = request_json(session, "/markets", query)
        rows.extend(payload.get("markets", []))
        cursor = payload.get("cursor")
        if not cursor:
            return rows


def parse_game_code(event_ticker: str) -> tuple[str, str] | None:
    suffix = event_ticker.split("-", 1)[-1]
    match = re.fullmatch(r"\d{2}[A-Z]{3}\d{2}([A-Z]+)", suffix)
    if not match:
        return None
    pair = match.group(1)
    for away in TEAM_CODES:
        if pair.startswith(away) and pair[len(away):] in TEAM_CODES:
            return away, pair[len(away):]
    return None


def requested_pair(value: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"[^A-Za-z]+", value.upper()) if p]
    if len(parts) != 2:
        raise SystemExit("--game must look like NE_SEA or NE@SEA")
    return parts[0], parts[1]


def discover(session: requests.Session, args: argparse.Namespace) -> list[dict]:
    today_et = datetime.now(ET).date()
    wanted = requested_pair(args.game) if args.game else None
    found = []
    for prop_type, series_ticker in SERIES.items():
        for market in paged_markets(
            session, series_ticker=series_ticker, status="open"
        ):
            pair = parse_game_code(market["event_ticker"])
            kickoff = pd.to_datetime(market.get("occurrence_datetime"), utc=True)
            if pair is None or pd.isna(kickoff):
                continue
            aliases = {"JAX": "JAC", "LAR": "LA", "WSH": "WAS"}
            normalized = tuple(aliases.get(team, team) for team in pair)
            normalized_wanted = (
                tuple(aliases.get(team, team) for team in wanted) if wanted else None
            )
            if wanted and normalized != normalized_wanted:
                continue
            if args.today and kickoff.tz_convert(ET).date() != today_et:
                continue
            found.append(
                {
                    "prop_type": prop_type,
                    "series_ticker": series_ticker,
                    "event_ticker": market["event_ticker"],
                    "away_team": pair[0],
                    "home_team": pair[1],
                    "kickoff": kickoff,
                }
            )
    return pd.DataFrame(found).drop_duplicates().to_dict("records") if found else []


def dollars(market: dict, field: str) -> float | None:
    value = market.get(f"{field}_dollars")
    if value is not None:
        return float(value)
    value = market.get(field)
    return None if value is None else float(value) / 100


def snapshot_rows(
    session: requests.Session, event: dict, timestamp: pd.Timestamp
) -> tuple[list[dict], list[dict]]:
    markets = paged_markets(session, event_ticker=event["event_ticker"])
    rows = []
    for market in markets:
        bid = dollars(market, "yes_bid")
        ask = dollars(market, "yes_ask")
        valid = bid is not None and ask is not None and 0 <= bid <= ask <= 1
        kickoff = event["kickoff"]
        rows.append(
            {
                "timestamp_utc": timestamp,
                "timestamp_et": timestamp.tz_convert(ET).isoformat(),
                "game_id": f"{kickoff.tz_convert(ET):%Y-%m-%d}_{event['away_team']}_{event['home_team']}",
                "away_team": event["away_team"],
                "home_team": event["home_team"],
                "kickoff": kickoff,
                "player": market.get("yes_sub_title", market.get("title", "")).split(":")[0].strip(),
                "player_source_id": market.get("custom_strike", {}).get("football_player"),
                "prop_type": event["prop_type"],
                "threshold": market.get("floor_strike"),
                "ticker": market["ticker"],
                "event_ticker": market["event_ticker"],
                "market_open_time": pd.to_datetime(market.get("open_time"), utc=True),
                "yes_bid": bid if valid else None,
                "yes_ask": ask if valid else None,
                "yes_bid_size": float(market["yes_bid_size_fp"]) if market.get("yes_bid_size_fp") is not None else None,
                "yes_ask_size": float(market["yes_ask_size_fp"]) if market.get("yes_ask_size_fp") is not None else None,
                "midpoint": (bid + ask) / 2 if valid else None,
                "trade_price": dollars(market, "last_price"),
                "spread": ask - bid if valid else None,
                "volume": float(market["volume_fp"]) if market.get("volume_fp") is not None else None,
                "volume_24h": float(market["volume_24h_fp"]) if market.get("volume_24h_fp") is not None else None,
                "open_interest": float(market["open_interest_fp"]) if market.get("open_interest_fp") is not None else None,
                "hours_to_kickoff": (kickoff - timestamp).total_seconds() / 3600,
                "minutes_to_kickoff": (kickoff - timestamp).total_seconds() / 60,
                "is_pregame": timestamp < kickoff,
                "market_status": market.get("status"),
            }
        )
    return rows, markets


def cents(value: object) -> str:
    return "—" if pd.isna(value) else f"{float(value) * 100:.1f}¢"


def print_snapshot(frame: pd.DataFrame, display_players: int) -> None:
    first = frame.iloc[0]
    timing = (
        f"{first.minutes_to_kickoff:.0f} min until kickoff"
        if first.is_pregame
        else f"{abs(first.minutes_to_kickoff):.0f} min after kickoff (NOT pregame)"
    )
    print(f"\n{first.away_team} @ {first.home_team} — {timing} — {first.timestamp_et}")
    for prop_type, heading in [("receiving_yards", "RECEIVING"), ("rushing_yards", "RUSHING")]:
        prop = frame[frame.prop_type.eq(prop_type)]
        if prop.empty:
            continue
        print(f"\n{heading}")
        ranked = (
            prop.groupby("player").volume.max().sort_values(ascending=False).index.tolist()
        )
        for player in ranked[:display_players]:
            print(f"\n{player}")
            ladder = prop[prop.player.eq(player)].sort_values("threshold")
            for row in ladder.itertuples(index=False):
                print(
                    f"  > {row.threshold:g}  bid {cents(row.yes_bid):>6}  "
                    f"ask {cents(row.yes_ask):>6}  mid {cents(row.midpoint):>6}  "
                    f"vol {row.volume:,.2f}  OI {row.open_interest:,.2f}"
                )
        remaining = len(ranked) - display_players
        if remaining > 0:
            print(f"\n  ... {remaining} more players saved to Parquet")


def game_folder(out: Path, event: dict) -> Path:
    kickoff_et = event["kickoff"].tz_convert(ET)
    return out / f"{kickoff_et:%Y-%m-%d}_{event['away_team']}_{event['home_team']}"


def save_snapshot(out: Path, events: list[dict], frame: pd.DataFrame, raw: dict) -> None:
    event = events[0]
    folder = game_folder(out, event)
    snapshots = folder / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    stamp = frame.timestamp_utc.iloc[0].strftime("%Y%m%dT%H%M%SZ")
    frame = frame.drop_duplicates(["ticker", "timestamp_utc"])
    frame.to_parquet(snapshots / f"{stamp}.parquet", index=False)
    with gzip.open(snapshots / f"{stamp}.json.gz", "wt") as handle:
        json.dump(raw, handle)
    catalog_columns = [
        "game_id", "away_team", "home_team", "kickoff", "player",
        "player_source_id", "prop_type", "threshold", "ticker", "event_ticker",
        "market_open_time",
    ]
    frame[catalog_columns].drop_duplicates("ticker").to_parquet(
        folder / "market_catalog.parquet", index=False
    )
    print(f"Saved {len(frame)} markets -> {snapshots / f'{stamp}.parquet'}", flush=True)


def main() -> None:
    args = parse_args()
    session = requests.Session()
    events = discover(session, args)
    if not events:
        raise SystemExit("No matching open receiving/rushing Kalshi events found.")
    if args.kickoff:
        kickoff = pd.Timestamp(args.kickoff)
        kickoff = kickoff.tz_localize("UTC") if kickoff.tz is None else kickoff.tz_convert("UTC")
        for event in events:
            event["kickoff"] = kickoff
    else:
        print(
            "Warning: using Kalshi occurrence_datetime as kickoff; pass --kickoff from an official schedule for training labels.",
            flush=True,
        )
    games = {(e["away_team"], e["home_team"]) for e in events}
    if len(games) != 1:
        raise SystemExit("--today currently requires one NFL game; use --game for a specific matchup.")
    counts = pd.DataFrame(events).groupby("prop_type").event_ticker.nunique().to_dict()
    print(f"Discovered {len(events)} prop events for {games.pop()}: {counts}", flush=True)

    collected = False
    while True:
        timestamp = pd.Timestamp.now(tz="UTC").floor("s")
        earliest_kickoff = min(e["kickoff"] for e in events)
        if (
            collected
            and args.continue_after_kickoff
            and timestamp >= earliest_kickoff + pd.Timedelta(hours=args.stop_hours_after_kickoff)
        ):
            print("Post-kickoff safety cutoff reached; stopping.", flush=True)
            break
        if collected and not args.continue_after_kickoff and timestamp >= earliest_kickoff:
            print("Scheduled kickoff reached; stopping before another API snapshot.", flush=True)
            break
        all_rows, raw = [], {}
        for event in events:
            rows, markets = snapshot_rows(session, event, timestamp)
            all_rows.extend(rows)
            raw[event["event_ticker"]] = markets
        frame = pd.DataFrame(all_rows).sort_values(
            ["prop_type", "player", "threshold"]
        )
        save_snapshot(args.out, events, frame, raw)
        print_snapshot(frame, args.display_players)
        collected = True
        if (
            args.continue_after_kickoff
            and timestamp >= earliest_kickoff
            and not frame.market_status.isin(["active", "open"]).any()
        ):
            print("All markets are closed; stopping after the final snapshot.", flush=True)
            break
        if args.once or (not args.continue_after_kickoff and timestamp >= earliest_kickoff):
            break
        time.sleep(max(args.interval, 1))


if __name__ == "__main__":
    main()
