"""Print real Kalshi player-prop histories and same-game threshold ladders."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--player", required=True, help="Full or partial player name")
    parser.add_argument(
        "--prop", required=True, choices=["receiving_yards", "rushing_yards"]
    )
    parser.add_argument("--game", help="Substring of game ID or matchup, e.g. CLE_CIN")
    parser.add_argument("--week", type=int)
    parser.add_argument(
        "--as-of",
        help="UTC/offset timestamp for ladder prices; defaults to each market's final pregame quote",
    )
    parser.add_argument("--interval-hours", type=float, default=4.0)
    parser.add_argument("--material-move", type=float, default=0.05)
    parser.add_argument("--max-points", type=int, default=14)
    parser.add_argument("--limit-markets", type=int, default=12)
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--table",
        type=Path,
        default=Path("data/processed/kalshi_player_prop_history.parquet"),
    )
    return parser.parse_args()


def name_key(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.lower())


def cents(value: object) -> str:
    return "—" if pd.isna(value) else f"{100 * float(value):.1f}¢"


def number(value: object) -> str:
    return "—" if pd.isna(value) else f"{float(value):,.0f}"


def stamp(value: object) -> str:
    if pd.isna(value):
        return "—"
    return pd.Timestamp(value).tz_convert("UTC").strftime("%Y-%m-%d %H:%MZ")


def choose_path_points(
    frame: pd.DataFrame, interval_hours: float, material_move: float, maximum: int
) -> pd.DataFrame:
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    if len(frame) <= maximum:
        return frame

    regular = frame.groupby(
        frame.timestamp.dt.floor(f"{max(interval_hours, 0.25)}h")
    ).tail(1)
    change = frame.midpoint.diff().abs().ge(material_move)
    extrema = frame.midpoint.isin([frame.midpoint.min(), frame.midpoint.max()])
    selected = pd.concat(
        [frame.iloc[[0, -1]], regular, frame[change | extrema]], ignore_index=False
    ).sort_values("timestamp").drop_duplicates("timestamp")

    if len(selected) > maximum:
        positions = np.linspace(0, len(selected) - 1, maximum, dtype=int)
        selected = selected.iloc[np.unique(positions)]
    return selected


def display_path(frame: pd.DataFrame, args: argparse.Namespace) -> None:
    shown = choose_path_points(
        frame, args.interval_hours, args.material_move, args.max_points
    ).copy()
    shown["timestamp"] = shown.timestamp.map(stamp)
    for column in ["yes_bid", "yes_ask", "midpoint", "trade_price", "spread"]:
        shown[column] = shown[column].map(cents)
    shown["volume"] = shown.volume.map(number)
    shown["open_interest"] = shown.open_interest.map(number)
    print(
        shown[
            [
                "timestamp",
                "yes_bid",
                "yes_ask",
                "midpoint",
                "trade_price",
                "spread",
                "volume",
                "open_interest",
            ]
        ].to_string(index=False)
    )


def quote_at(frame: pd.DataFrame, as_of: pd.Timestamp | None) -> pd.Series | None:
    before = frame[frame.timestamp.lt(frame.kickoff)]
    if as_of is not None:
        before = before[before.timestamp.le(as_of)]
    before = before[before.midpoint.notna()].sort_values("timestamp")
    return None if before.empty else before.iloc[-1]


def main() -> None:
    args = parse_args()
    if not args.table.exists():
        raise SystemExit(
            f"Missing {args.table}. Run scripts/build_kalshi_canonical.py first."
        )
    markets = pd.read_parquet(args.raw / "kalshi_market_player_matches.parquet")
    player_query = name_key(args.player)
    player_match = markets.player.map(name_key).str.contains(player_query, regex=False)
    selected = markets[player_match & markets.prop_type.eq(args.prop)].copy()
    if args.game:
        game_query = args.game.lower()
        selected = selected[
            selected.game_id.str.lower().str.contains(game_query, regex=False)
            | selected.game.str.lower().str.contains(game_query, regex=False)
        ]
    if args.week is not None:
        selected = selected[
            selected.game_id.str.split("_").str[1].astype(int).eq(args.week)
        ]
    if selected.empty:
        choices = sorted(markets.loc[player_match, "player"].unique())
        suffix = f" Player matches: {', '.join(choices)}" if choices else ""
        raise SystemExit(f"No matching Kalshi markets.{suffix}")

    market_ids = selected.market_id.tolist()
    history = pd.read_parquet(
        args.table, filters=[("market_id", "in", market_ids)]
    )
    history["timestamp"] = pd.to_datetime(history.timestamp, utc=True)
    history["kickoff"] = pd.to_datetime(history.kickoff, utc=True)
    available_ids = set(history.market_id.unique())
    selected = selected[selected.market_id.isin(available_ids)].copy()
    if selected.empty:
        raise SystemExit("Matching markets exist, but their history is not downloaded yet.")

    volume = history.groupby("market_id").volume.sum(min_count=1).rename("history_volume")
    selected = selected.join(volume, on="market_id")
    selected = selected.sort_values(
        ["kickoff", "history_volume", "threshold"], ascending=[False, False, True]
    )
    total = len(selected)
    selected = selected.head(args.limit_markets)
    selected_ids = set(selected.market_id)
    display_history = history[history.market_id.isin(selected_ids)]
    print(
        f"Found {total} downloaded {args.prop} markets for "
        f"{', '.join(sorted(selected.player.unique()))}; showing {len(selected)}."
    )
    print("All timestamps are UTC. Prices are YES prices; midpoint=(bid+ask)/2.")

    for market in selected.itertuples(index=False):
        frame = display_history[display_history.market_id.eq(market.market_id)]
        pregame = frame[frame.is_pregame & frame.midpoint.notna()].sort_values("timestamp")
        if pregame.empty:
            continue
        first, last = pregame.iloc[0], pregame.iloc[-1]
        tight = pregame[pregame.spread.le(0.10)]
        first_tight = tight.iloc[0] if len(tight) else None
        identity = frame.iloc[0]
        print("\n" + "=" * 100)
        print(
            f"{market.player} | {market.game} | kickoff {stamp(market.kickoff)} | "
            f"{market.prop_type} > {market.threshold:g}"
        )
        print(
            f"Team: {identity.team if pd.notna(identity.team) else '—'} | "
            f"opponent: {identity.opponent if pd.notna(identity.opponent) else '—'}"
        )
        print(f"Ticker: {market.market_id}")
        print(
            f"Opened: {stamp(market.open_time)} | actual: {number(market.actual_stat)} yards | "
            f"observations: {len(pregame):,} pregame / {len(frame):,} total"
        )
        print(
            f"First: {cents(first.midpoint)} | last pregame: {cents(last.midpoint)} | "
            f"low: {cents(pregame.midpoint.min())} | high: {cents(pregame.midpoint.max())}"
        )
        if first_tight is not None:
            print(
                f"First quote with spread <=10¢: {cents(first_tight.midpoint)} "
                f"at {stamp(first_tight.timestamp)}"
            )
        print("Price path (sampled every few hours plus material moves/extrema):")
        display_path(pregame, args)

    as_of = pd.Timestamp(args.as_of) if args.as_of else None
    if as_of is not None:
        as_of = as_of.tz_localize("UTC") if as_of.tz is None else as_of.tz_convert("UTC")
    print("\n" + "=" * 100)
    label = f"latest quote at or before {stamp(as_of)}" if as_of else "final pregame quote"
    print(f"Threshold ladders ({label}):")
    games = selected[["game_id", "player", "game", "kickoff"]].drop_duplicates()
    for game in games.itertuples(index=False):
        ladder = history[
            history.game_id.eq(game.game_id)
            & history.player.eq(game.player)
            & history.prop_type.eq(args.prop)
        ]
        rows = []
        for threshold, threshold_frame in ladder.groupby("threshold"):
            quote = quote_at(threshold_frame, as_of)
            if quote is not None:
                rows.append((threshold, quote.midpoint, quote.yes_bid, quote.yes_ask, quote.timestamp))
        if not rows:
            continue
        print(f"\n{game.player} {args.prop} | {game.game} | kickoff {stamp(game.kickoff)}")
        for threshold, midpoint, bid, ask, timestamp in sorted(rows):
            print(
                f"  > {threshold:g}: {cents(midpoint)} "
                f"(bid {cents(bid)} / ask {cents(ask)}, {stamp(timestamp)})"
            )


if __name__ == "__main__":
    main()
