"""Exact-threshold as-of join of sportsbook history to audited Kalshi/NFL rows."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import joblib
import pandas as pd


DEFAULT_GAMES = ["2025_13_LA_CAR", "2025_16_PIT_DET"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", nargs="+", default=DEFAULT_GAMES)
    parser.add_argument(
        "--kalshi", type=Path,
        default=Path("data/processed/kalshi_pbp_pilot_audited.parquet"),
    )
    parser.add_argument(
        "--sportsbook", type=Path,
        default=Path("data/processed/sportsbook_prop_history_sample.parquet"),
    )
    parser.add_argument(
        "--model", type=Path, default=Path("models/nfl_fair_value_v0.joblib")
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("data/processed/kalshi_sportsbook_join_sample.parquet"),
    )
    parser.add_argument("--max-age-minutes", type=float, default=6.0)
    return parser.parse_args()


def name_key(value: str) -> str:
    value = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", value.lower())
    return re.sub(r"[^a-z0-9]", "", value)


def add_v0_probability(frame: pd.DataFrame, model_path: Path) -> pd.DataFrame:
    bundle = joblib.load(model_path)
    frame = frame.copy()
    frame["prop_receiving"] = frame.prop_type.eq("receiving_yards").astype(float)
    home_team = frame.game_id.str.split("_").str[-1]
    frame["player_score_differential"] = frame.score_differential.where(
        frame.team.eq(home_team), -frame.score_differential
    )
    features = bundle["features"]
    x = frame[features].apply(pd.to_numeric, errors="coerce").fillna(0)
    frame["v0_fair_yes_probability"] = bundle["model"].predict_proba(x)[:, 1]
    frame.loc[frame.yards_so_far.gt(frame.threshold), "v0_fair_yes_probability"] = 0.999
    return frame


def main() -> None:
    args = parse_args()
    kalshi = pd.read_parquet(args.kalshi)
    sportsbook = pd.read_parquet(args.sportsbook)
    kalshi = kalshi[kalshi.game_id.isin(args.games)].copy()
    sportsbook = sportsbook[
        sportsbook.game_id.isin(args.games)
        & sportsbook.side.eq("over")
        & sportsbook.consensus_fair_probability.notna()
    ].copy()
    kalshi["timestamp"] = pd.to_datetime(kalshi.timestamp, utc=True).astype(
        "datetime64[ns, UTC]"
    )
    sportsbook["snapshot_timestamp"] = pd.to_datetime(
        sportsbook.snapshot_timestamp, utc=True
    ).astype("datetime64[ns, UTC]")
    kalshi["player_key"] = kalshi.player.map(name_key)
    sportsbook["player_key"] = sportsbook.player.map(name_key)
    kalshi["threshold_key"] = (kalshi.threshold * 2).round().astype("Int64")
    sportsbook["threshold_key"] = (sportsbook.threshold * 2).round().astype("Int64")

    group = ["game_id", "snapshot_timestamp", "player_key", "prop_type", "threshold_key"]
    consensus = sportsbook.groupby(group, as_index=False).agg(
        sportsbook_player=("player", "first"),
        sportsbook_threshold=("threshold", "first"),
        sportsbook_fair_yes_probability=("consensus_fair_probability", "first"),
        sportsbook_book_count=("consensus_book_count", "max"),
        sportsbook_books=("bookmaker", lambda value: ", ".join(sorted(set(value)))),
    ).rename(columns={"snapshot_timestamp": "sportsbook_timestamp"})

    by = ["game_id", "player_key", "prop_type", "threshold_key"]
    left = kalshi.sort_values(["timestamp"] + by)
    right = consensus.sort_values(["sportsbook_timestamp"] + by)
    joined = pd.merge_asof(
        left,
        right,
        left_on="timestamp",
        right_on="sportsbook_timestamp",
        by=by,
        direction="backward",
        tolerance=pd.Timedelta(minutes=args.max_age_minutes),
    )
    joined = joined[joined.sportsbook_timestamp.notna()].copy()
    if joined.empty:
        raise SystemExit("No exact player/prop/threshold matches were found; no output written.")
    joined["sportsbook_age_seconds"] = (
        joined.timestamp - joined.sportsbook_timestamp
    ).dt.total_seconds()
    if (joined.sportsbook_age_seconds < 0).any():
        raise RuntimeError("Future sportsbook data entered the as-of join.")
    joined = add_v0_probability(joined, args.model)
    joined["kalshi_no_ask"] = 1 - joined.yes_bid
    joined["sportsbook_yes_edge_vs_ask"] = (
        joined.sportsbook_fair_yes_probability - joined.yes_ask
    )
    joined["sportsbook_no_edge_vs_ask"] = (
        (1 - joined.sportsbook_fair_yes_probability) - joined.kalshi_no_ask
    )
    joined["v0_minus_sportsbook"] = (
        joined.v0_fair_yes_probability - joined.sportsbook_fair_yes_probability
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    joined.to_parquet(args.output, index=False)

    possible = kalshi[["game_id", "player_key", "prop_type", "threshold_key"]].drop_duplicates()
    offered = consensus[["game_id", "player_key", "prop_type", "threshold_key"]].drop_duplicates()
    exact = possible.merge(offered, on=by, how="inner")
    print(f"Saved {len(joined):,} matched Kalshi/NFL observations to {args.output}")
    print(f"Games: {joined.game_id.nunique()} | Kalshi markets: {joined.market_id.nunique()}")
    print(
        f"Exact threshold coverage: {len(exact):,}/{len(possible):,} "
        f"Kalshi player-prop thresholds ({len(exact) / len(possible):.1%})"
    )
    print(
        f"Sportsbook age: median {joined.sportsbook_age_seconds.median():.0f}s, "
        f"max {joined.sportsbook_age_seconds.max():.0f}s"
    )
    print(
        f"Books per matched consensus: median {joined.sportsbook_book_count.median():.0f}, "
        f"max {joined.sportsbook_book_count.max():.0f}"
    )


if __name__ == "__main__":
    main()
