"""Quickly inspect Kalshi receiving/rushing history for one NFL game."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", required=True, help="Game ID, e.g. 2025_13_LA_CAR")
    parser.add_argument(
        "--table",
        type=Path,
        default=Path("data/processed/kalshi_player_prop_history.parquet"),
    )
    return parser.parse_args()


def spacing(frame: pd.DataFrame) -> dict[str, float]:
    gaps = frame.timestamp.drop_duplicates().sort_values().diff().dropna().dt.total_seconds() / 60
    if gaps.empty:
        return {key: np.nan for key in ["p25", "median", "p75", "max", "le1", "le2", "le5"]}
    return {
        "p25": gaps.quantile(0.25),
        "median": gaps.median(),
        "p75": gaps.quantile(0.75),
        "max": gaps.max(),
        "le1": 100 * gaps.le(1).mean(),
        "le2": 100 * gaps.le(2).mean(),
        "le5": 100 * gaps.le(5).mean(),
    }


def market_metrics(frame: pd.DataFrame, kickoff: pd.Timestamp) -> dict[str, object]:
    frame = frame.sort_values("timestamp")
    post = frame[frame.timestamp.between(kickoff, kickoff + pd.Timedelta(hours=4))]
    all_spacing = spacing(frame)
    post_spacing = spacing(post)
    mids = frame.midpoint.dropna()
    changes_cents = mids.mul(100).round(4).diff().abs().dropna()
    usable_mids = frame.loc[frame.spread.le(0.20), "midpoint"].dropna()
    usable_changes_cents = usable_mids.mul(100).round(4).diff().abs().dropna()
    both = frame.yes_bid.notna() & frame.yes_ask.notna()
    return {
        "player": frame.player.iloc[0],
        "prop": frame.prop_type.iloc[0].replace("_yards", ""),
        "threshold": frame.threshold.iloc[0],
        "ticker": frame.ticker.iloc[0],
        "obs": len(frame),
        "post_obs": len(post),
        "med_min": all_spacing["median"],
        "p25_min": all_spacing["p25"],
        "p75_min": all_spacing["p75"],
        "max_min": all_spacing["max"],
        "le_1m_pct": all_spacing["le1"],
        "le_2m_pct": all_spacing["le2"],
        "le_5m_pct": all_spacing["le5"],
        "post_med": post_spacing["median"],
        "post_p25": post_spacing["p25"],
        "post_p75": post_spacing["p75"],
        "post_max": post_spacing["max"],
        "post_le1": post_spacing["le1"],
        "post_le2": post_spacing["le2"],
        "post_le5": post_spacing["le5"],
        "bid_obs": frame.yes_bid.notna().sum(),
        "ask_obs": frame.yes_ask.notna().sum(),
        "both_obs": both.sum(),
        "trade_obs": frame.trade_price.notna().sum(),
        "volume_obs": frame.volume.gt(0).sum(),
        "mid_prices": mids.round(4).nunique(),
        "changes_1c": changes_cents.ge(1).sum(),
        "changes_3c": changes_cents.ge(3).sum(),
        "median_spread": frame.spread.median(),
        "post_spread": post.spread.median(),
        "price_min": mids.min(),
        "price_max": mids.max(),
        "usable_price_min": usable_mids.min(),
        "usable_price_max": usable_mids.max(),
        "usable_changes_1c": usable_changes_cents.ge(1).sum(),
    }


def fmt_time(value: object) -> str:
    return "-" if pd.isna(value) else pd.Timestamp(value).strftime("%Y-%m-%d %H:%MZ")


def print_coverage(data: pd.DataFrame, kickoff: pd.Timestamp) -> None:
    rows = []
    for (player, prop), frame in data.groupby(["player", "prop_type"], sort=True):
        thresholds = sorted(frame.threshold.unique())
        rows.append(
            {
                "player": player,
                "prop": prop.replace("_yards", ""),
                "n_thr": len(thresholds),
                "thresholds": ", ".join(f">{x:g}" for x in thresholds),
                "obs": len(frame),
                "pre": frame.timestamp.lt(kickoff).sum(),
                "post": frame.timestamp.between(kickoff, kickoff + pd.Timedelta(hours=4)).sum(),
                "earliest": fmt_time(frame.timestamp.min()),
                "latest": fmt_time(frame.timestamp.max()),
            }
        )
    print("\nPLAYER / PROP COVERAGE")
    print(pd.DataFrame(rows).to_string(index=False))


def print_market_tables(metrics: pd.DataFrame) -> None:
    sampling = metrics[
        [
            "player", "prop", "threshold", "obs", "post_obs", "med_min", "p25_min",
            "p75_min", "max_min", "le_1m_pct", "le_2m_pct", "le_5m_pct",
            "post_med", "post_p25", "post_p75", "post_max", "post_le1", "post_le2", "post_le5",
        ]
    ].copy()
    print("\nSAMPLING BY MARKET (spacing in minutes; percentages are consecutive gaps)")
    print(sampling.round(1).to_string(index=False))

    activity = metrics[
        [
            "player", "prop", "threshold", "bid_obs", "ask_obs", "both_obs",
            "trade_obs", "volume_obs", "mid_prices", "changes_1c", "changes_3c",
            "median_spread", "post_spread",
        ]
    ].copy()
    activity["median_spread"] *= 100
    activity["post_spread"] *= 100
    print("\nQUOTE / TRADE ACTIVITY BY MARKET (spreads in cents)")
    print(activity.round(1).to_string(index=False))


def choose_examples(metrics: pd.DataFrame) -> pd.DataFrame:
    preferred = {
        "Puka Nacua", "Davante Adams", "Kyren Williams", "Matthew Stafford",
        "Bryce Young", "Chuba Hubbard", "Rico Dowdle", "Tetairoa McMillan", "Blake Corum",
    }
    ranked = metrics.copy()
    spread = ranked.post_spread.fillna(1).clip(lower=0.01)
    ranked["score"] = (
        ranked.post_obs.rank(pct=True)
        + ranked.usable_changes_1c.rank(pct=True)
        + ranked.trade_obs.rank(pct=True)
        + 1.5 * (1 / spread).rank(pct=True)
        + ranked.player.isin(preferred).astype(float)
    )
    ranked = ranked.sort_values("score", ascending=False)
    picked = []
    seen = set()
    for row in ranked.itertuples(index=False):
        key = row.player
        if key in seen:
            continue
        picked.append(row.ticker)
        seen.add(key)
        if len(picked) == 5:
            break
    return metrics.set_index("ticker").loc[picked].reset_index()


def nearest_snapshot(frame: pd.DataFrame, target: pd.Timestamp) -> pd.Series | None:
    frame = frame.copy()
    frame["distance"] = (frame.timestamp - target).abs()
    if frame.empty:
        return None
    point = frame.sort_values("distance").iloc[0]
    return None if point.distance > pd.Timedelta(minutes=20) else point


def print_examples(data: pd.DataFrame, metrics: pd.DataFrame, kickoff: pd.Timestamp) -> None:
    examples = choose_examples(metrics)
    print("\nAUTOMATICALLY SELECTED EXAMPLES")
    for metric in examples.itertuples(index=False):
        frame = data[data.ticker.eq(metric.ticker)].sort_values("timestamp")
        print("\n" + "=" * 105)
        print(
            f"{metric.player} | {metric.prop} >{metric.threshold:g} | {metric.ticker}\n"
            f"observations {metric.obs:,} ({metric.post_obs:,} post-kickoff) | "
            f"median interval {metric.med_min:.1f}m | trade observations {metric.trade_obs:,} | "
            f">=1c changes {metric.changes_1c:,} | usable-quote range "
            f"{100*metric.usable_price_min:.1f}-{100*metric.usable_price_max:.1f}c | "
            f"median spread {100*metric.median_spread:.1f}c"
        )
        sample = []
        for minutes in [-15, 0, 15, 60, 120, 180, 240]:
            point = nearest_snapshot(frame, kickoff + pd.Timedelta(minutes=minutes))
            if point is None:
                continue
            sample.append(
                {
                    "target": f"T{minutes:+}m",
                    "actual_time": point.timestamp.strftime("%H:%MZ"),
                    "bid_c": 100 * point.yes_bid,
                    "ask_c": 100 * point.yes_ask,
                    "mid_c": 100 * point.midpoint,
                    "trade_c": 100 * point.trade_price,
                    "spread_c": 100 * point.spread,
                    "volume": point.volume,
                    "open_interest": point.open_interest,
                }
            )
        print(pd.DataFrame(sample).round(1).to_string(index=False))


def main() -> None:
    args = parse_args()
    data = pd.read_parquet(args.table, filters=[("game_id", "==", args.game)])
    if data.empty:
        raise SystemExit(f"No canonical rows found for {args.game}")
    data = data[data.prop_type.isin(["receiving_yards", "rushing_yards"])].copy()
    data["timestamp"] = pd.to_datetime(data.timestamp, utc=True)
    data["kickoff"] = pd.to_datetime(data.kickoff, utc=True)
    kickoff = data.kickoff.dropna().iloc[0]
    post = data.timestamp.between(kickoff, kickoff + pd.Timedelta(hours=4))
    teams = " / ".join(sorted(set(data.team.dropna()) | set(data.opponent.dropna())))
    print(f"GAME: {args.game} ({data.game.dropna().iloc[0]}; teams {teams})")
    print(f"Kickoff: {fmt_time(kickoff)}")
    print(f"Markets: {data.market_id.nunique():,} total | "
          f"{data.loc[data.prop_type.eq('receiving_yards'), 'market_id'].nunique():,} receiving | "
          f"{data.loc[data.prop_type.eq('rushing_yards'), 'market_id'].nunique():,} rushing")
    print(f"Players: {data.player.nunique():,} | player/game/prop combinations: "
          f"{len(data[['player', 'game_id', 'prop_type']].drop_duplicates()):,}")
    print(f"Observations: {len(data):,} total | {data.timestamp.lt(kickoff).sum():,} pregame | "
          f"{post.sum():,} kickoff through T+4h")

    print_coverage(data, kickoff)
    metrics = pd.DataFrame(
        [market_metrics(frame, kickoff) for _, frame in data.groupby("market_id")]
    ).sort_values(["player", "prop", "threshold"])
    print_market_tables(metrics)
    print_examples(data, metrics, kickoff)


if __name__ == "__main__":
    main()
