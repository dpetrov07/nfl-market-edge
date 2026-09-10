"""Build the fixed eight-game Kalshi plus nflverse play-state pilot."""

from __future__ import annotations

import argparse
from pathlib import Path

import nflreadpy as nfl
import pandas as pd


PILOT_GAMES = [
    "2025_13_LA_CAR",
    "2025_05_KC_JAX",
    "2025_09_CHI_CIN",
    "2025_13_SF_CLE",
    "2025_16_PIT_DET",
    "2025_18_BAL_PIT",
    "2025_20_SF_SEA",
    "2025_22_SEA_NE",
]
PLAYER_ID_OVERRIDES = {
    "Kenneth Gainwell": "00-0036919",
    "Noah Gray": "00-0036637",
}
PLAYER_TEAM_OVERRIDES = {"Kenneth Gainwell": "PIT", "Noah Gray": "KC"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", nargs="+", default=PILOT_GAMES)
    parser.add_argument(
        "--kalshi",
        type=Path,
        default=Path("data/processed/kalshi_player_prop_history.parquet"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/processed/kalshi_pbp_pilot.parquet"),
    )
    return parser.parse_args()


def load_pilot_pbp(game_ids: list[str]) -> pd.DataFrame:
    season = int(game_ids[0].split("_", 1)[0])
    pbp = nfl.load_pbp(season)
    pbp = pbp.filter(pbp["game_id"].is_in(game_ids))
    columns = [
        "game_id", "play_id", "qtr", "time", "game_seconds_remaining",
        "posteam", "yardline_100", "yrdln", "down", "ydstogo",
        "total_home_score", "total_away_score", "score_differential_post",
        "pass_attempt", "rush_attempt", "complete_pass",
        "receiver_player_id", "receiving_yards",
        "rusher_player_id", "rushing_yards", "time_of_day",
        "end_clock_time", "order_sequence", "play_type", "two_point_attempt",
        "home_score", "away_score", "desc",
    ]
    return pbp.select(columns).to_pandas()


def one(value: object) -> bool:
    return pd.notna(value) and float(value) == 1


def number(value: object, default: float = 0.0) -> float:
    return float(value) if pd.notna(value) else default


def cumulative_states(
    pbp: pd.DataFrame, players: pd.DataFrame, kickoff: pd.Timestamp
) -> tuple[pd.DataFrame, dict[str, object]]:
    pbp = pbp.copy()
    pbp["play_start_timestamp"] = pd.to_datetime(
        pbp.time_of_day, utc=True, format="mixed", errors="coerce"
    )
    pbp["play_end_timestamp"] = pd.to_datetime(
        pbp.end_clock_time, utc=True, format="mixed", errors="coerce"
    )
    # In the current nflverse feed these are UTC wall-clock timestamps. A few
    # plays lack an end timestamp. Do not expose those results at the snap:
    # wait for the next recorded event, or 30 seconds for the terminal play.
    timed = pbp[pbp.play_start_timestamp.notna()].sort_values(
        ["play_start_timestamp", "order_sequence", "play_id"]
    ).copy()
    valid_end = timed.play_end_timestamp.ge(timed.play_start_timestamp)
    next_start = timed.play_start_timestamp.shift(-1)
    timed["state_available_at"] = timed.play_end_timestamp.where(valid_end)
    timed["state_time_source"] = "end_clock_time"
    fallback = timed.state_available_at.isna() & next_start.notna()
    timed.loc[fallback, "state_available_at"] = next_start[fallback]
    timed.loc[fallback, "state_time_source"] = "next_play_start_fallback"
    terminal = timed.state_available_at.isna()
    timed.loc[terminal, "state_available_at"] = (
        timed.loc[terminal, "play_start_timestamp"] + pd.Timedelta(seconds=30)
    )
    timed.loc[terminal, "state_time_source"] = "terminal_30s_fallback"
    timed["timing_safe"] = True

    player_rows = players[["player", "player_id", "team"]].drop_duplicates("player_id")
    identity = player_rows.set_index("player_id").to_dict("index")
    player_stats = {
        player_id: {"targets": 0, "receptions": 0, "receiving_yards": 0.0,
                    "carries": 0, "rushing_yards": 0.0}
        for player_id in identity
    }
    team_stats = {
        team: {"pass_attempts": 0, "rush_attempts": 0, "offensive_plays": 0}
        for team in player_rows.team.dropna().unique()
    }
    states = []

    def add_states(play: dict[str, object]) -> None:
        for player_id, info in identity.items():
            stats = player_stats[player_id]
            team = team_stats[info["team"]]
            targets = stats["targets"]
            carries = stats["carries"]
            states.append(
                {
                    **play,
                    "player": info["player"],
                    "player_id": player_id,
                    "team": info["team"],
                    "team_pass_attempts_so_far": team["pass_attempts"],
                    "team_rush_attempts_so_far": team["rush_attempts"],
                    "team_offensive_plays_so_far": team["offensive_plays"],
                    "targets_so_far": targets,
                    "receptions_so_far": stats["receptions"],
                    "receiving_yards_so_far": stats["receiving_yards"],
                    "carries_so_far": carries,
                    "rushing_yards_so_far": stats["rushing_yards"],
                    "target_share_so_far": (
                        targets / team["pass_attempts"] if team["pass_attempts"] else 0.0
                    ),
                    "carry_share_so_far": (
                        carries / team["rush_attempts"] if team["rush_attempts"] else 0.0
                    ),
                }
            )

    add_states(
        {
            "play_id": 0.0,
            "play_start_timestamp": kickoff,
            "state_available_at": kickoff,
            "state_time_source": "scheduled_kickoff_seed",
            "timing_safe": True,
            "play_timestamp": kickoff,
            "play_timestamp_source": "scheduled_kickoff_seed",
            "quarter": 1,
            "game_clock": "15:00",
            "game_seconds_remaining": 3600.0,
            "possession": None,
            "home_score": 0.0,
            "away_score": 0.0,
            "score_differential": 0.0,
            "possession_score_differential": None,
            "down": None,
            "yards_to_go": None,
            "yardline_100": None,
            "yardline": None,
            "last_play": "Scheduled kickoff; no timed nflverse play yet",
        }
    )

    for row in timed.itertuples(index=False):
        possession = row.posteam if pd.notna(row.posteam) else None
        # Official weekly player stats do not count two-point conversion
        # targets/carries in these receiving/rushing categories.
        scrimmage_play = not one(row.two_point_attempt)
        pass_attempt = one(row.pass_attempt) and scrimmage_play
        rush_attempt = one(row.rush_attempt) and scrimmage_play
        if possession in team_stats:
            team_stats[possession]["pass_attempts"] += int(pass_attempt)
            team_stats[possession]["rush_attempts"] += int(rush_attempt)
            team_stats[possession]["offensive_plays"] += int(pass_attempt or rush_attempt)

        receiver_id = row.receiver_player_id if pd.notna(row.receiver_player_id) else None
        if receiver_id in player_stats and pass_attempt:
            player_stats[receiver_id]["targets"] += 1
            if one(row.complete_pass):
                player_stats[receiver_id]["receptions"] += 1
                player_stats[receiver_id]["receiving_yards"] += number(row.receiving_yards)

        rusher_id = row.rusher_player_id if pd.notna(row.rusher_player_id) else None
        if rusher_id in player_stats and rush_attempt:
            player_stats[rusher_id]["carries"] += 1
            player_stats[rusher_id]["rushing_yards"] += number(row.rushing_yards)

        home_score = number(row.total_home_score)
        away_score = number(row.total_away_score)
        add_states(
            {
                "play_id": row.play_id,
                "play_start_timestamp": row.play_start_timestamp,
                "state_available_at": row.state_available_at,
                "state_time_source": row.state_time_source,
                "timing_safe": row.timing_safe,
                # Keep the old names as aliases so downstream research code
                # written against the first pilot still works.
                "play_timestamp": row.state_available_at,
                "play_timestamp_source": row.state_time_source,
                "quarter": int(row.qtr) if pd.notna(row.qtr) else None,
                "game_clock": row.time,
                "game_seconds_remaining": row.game_seconds_remaining,
                "possession": possession,
                "home_score": home_score,
                "away_score": away_score,
                "score_differential": home_score - away_score,
                "possession_score_differential": row.score_differential_post,
                "down": row.down,
                "yards_to_go": row.ydstogo,
                "yardline_100": row.yardline_100,
                "yardline": row.yrdln,
                "last_play": row.desc,
            }
        )

    timing = {
        "total_plays": len(pbp),
        "timed_plays": len(timed),
        "end_timestamps": int(valid_end.sum()),
        "start_fallbacks": int(fallback.sum()),
        "terminal_fallbacks": int(terminal.sum()),
        "stat_play_fallbacks": int(
            ((fallback | terminal)
             & (timed.pass_attempt.eq(1) | timed.rush_attempt.eq(1))
             & ~timed.two_point_attempt.eq(1)).sum()
        ),
    }
    football = timed[~timed.play_type.eq("no_play")]
    timing["game_start"] = football.play_start_timestamp.min()
    timing["game_end"] = football.state_available_at.max()
    timing["final_home_score"] = number(pbp.total_home_score.max())
    timing["final_away_score"] = number(pbp.total_away_score.max())
    timing["official_home_score"] = number(pbp.home_score.dropna().iloc[0])
    timing["official_away_score"] = number(pbp.away_score.dropna().iloc[0])
    return pd.DataFrame(states), timing


def join_markets(markets: pd.DataFrame, states: pd.DataFrame) -> pd.DataFrame:
    left = markets.sort_values(["timestamp", "player_id"])
    right = states.sort_values(["state_available_at", "player_id"])
    joined = pd.merge_asof(
        left,
        right.drop(columns=["player", "team", "play_timestamp"]),
        left_on="timestamp",
        right_on="state_available_at",
        by="player_id",
        direction="backward",
    )
    joined["play_timestamp"] = joined.state_available_at
    joined["seconds_since_play"] = (
        joined.timestamp - joined.state_available_at
    ).dt.total_seconds()
    joined["minutes_since_play"] = joined.seconds_since_play / 60
    joined["yards_so_far"] = joined.receiving_yards_so_far.where(
        joined.prop_type.eq("receiving_yards"), joined.rushing_yards_so_far
    )
    joined["yards_to_threshold"] = joined.threshold - joined.yards_so_far
    columns = [
        "game_id", "timestamp", "market_id", "ticker", "player", "player_id",
        "team", "opponent", "prop_type", "threshold", "yes_bid", "yes_ask",
        "midpoint", "trade_price", "spread", "volume", "open_interest",
        "actual_result", "settlement_result", "kickoff",
        "play_id", "play_start_timestamp", "state_available_at",
        "state_time_source", "timing_safe", "play_timestamp",
        "play_timestamp_source", "seconds_since_play",
        "minutes_since_play", "quarter", "game_clock", "game_seconds_remaining",
        "possession", "home_score", "away_score", "score_differential",
        "possession_score_differential", "down", "yards_to_go", "yardline_100",
        "yardline", "team_pass_attempts_so_far", "team_rush_attempts_so_far",
        "team_offensive_plays_so_far", "targets_so_far", "receptions_so_far",
        "receiving_yards_so_far", "carries_so_far", "rushing_yards_so_far",
        "target_share_so_far", "carry_share_so_far", "yards_so_far",
        "yards_to_threshold", "last_play",
    ]
    return joined[columns].sort_values(["timestamp", "ticker"])


def print_examples(joined: pd.DataFrame, kickoff: pd.Timestamp) -> None:
    examples = [
        ("Puka Nacua", "receiving_yards", 79.5),
        ("Davante Adams", "receiving_yards", 59.5),
        ("Tetairoa McMillan", "receiving_yards", 39.5),
        ("Blake Corum", "rushing_yards", 59.5),
        ("Rico Dowdle", "rushing_yards", 49.5),
    ]
    targets = [0, 15, 60, 120, 150]
    for player, prop, threshold in examples:
        frame = joined[
            joined.player.eq(player)
            & joined.prop_type.eq(prop)
            & joined.threshold.eq(threshold)
        ]
        if frame.empty:
            continue
        rows = []
        for minute in targets:
            target = kickoff + pd.Timedelta(minutes=minute)
            point = frame.iloc[(frame.timestamp - target).abs().argsort()[:1]].iloc[0]
            rows.append(
                {
                    "target": f"T+{minute}m",
                    "timestamp": point.timestamp.strftime("%H:%MZ"),
                    "mid_c": 100 * point.midpoint,
                    "qtr": point.quarter,
                    "clock": point.game_clock,
                    "score": f"LA {int(point.away_score)}-CAR {int(point.home_score)}",
                    "poss": point.possession,
                    "targets": point.targets_so_far,
                    "rec": point.receptions_so_far,
                    "rec_yds": point.receiving_yards_so_far,
                    "carries": point.carries_so_far,
                    "rush_yds": point.rushing_yards_so_far,
                    "to_line": point.yards_to_threshold,
                    "since_play_s": point.seconds_since_play,
                }
            )
        print(f"\n{player} {prop} >{threshold:g} | {frame.ticker.iloc[0]}")
        print(pd.DataFrame(rows).round(1).to_string(index=False))


def quality_row(
    game_id: str, joined: pd.DataFrame, total_markets: int
) -> dict[str, object]:
    spacing = (
        joined.sort_values("timestamp")
        .groupby("market_id")
        .timestamp.apply(lambda values: values.diff().dt.total_seconds().median() / 60)
    )
    matched = joined.play_id.notna()
    ages = joined.loc[matched, "seconds_since_play"]
    return {
        "game_id": game_id,
        "markets": total_markets,
        "post_markets": joined.market_id.nunique(),
        "players": joined.player.nunique(),
        "post_obs": len(joined),
        "joined": int(matched.sum()),
        "join_pct": 100 * matched.mean(),
        "spacing_min": spacing.median(),
        "play_age_s": ages.median(),
        "within_1m_pct": 100 * ages.le(60).mean(),
        "within_2m_pct": 100 * ages.le(120).mean(),
        "spread_c": 100 * joined.spread.median(),
        "spread_le10_pct": 100 * joined.spread.le(0.10).mean(),
        "receiving_rows": int(joined.prop_type.eq("receiving_yards").sum()),
        "rushing_rows": int(joined.prop_type.eq("rushing_yards").sum()),
    }


def main() -> None:
    args = parse_args()
    markets = pd.read_parquet(
        args.kalshi,
        filters=[("game_id", "in", args.games)],
    )
    markets["timestamp"] = pd.to_datetime(markets.timestamp, utc=True)
    markets["kickoff"] = pd.to_datetime(markets.kickoff, utc=True)
    markets["player_id"] = markets.player.map(PLAYER_ID_OVERRIDES).fillna(
        markets.player_id
    )
    markets["team"] = markets.team.fillna(markets.player.map(PLAYER_TEAM_OVERRIDES))
    missing_opponent = markets.opponent.isna() & markets.team.notna()
    markets.loc[missing_opponent, "opponent"] = markets.loc[missing_opponent].apply(
        lambda row: next(
            team for team in row.game_id.split("_")[2:] if team != row.team
        ),
        axis=1,
    )
    pbp = load_pilot_pbp(args.games)
    joined_games = []
    quality = []

    for game_id in args.games:
        game_markets = markets[markets.game_id.eq(game_id)].copy()
        kickoff = game_markets.kickoff.iloc[0]
        total_markets = game_markets.market_id.nunique()
        game_markets = game_markets[
            game_markets.prop_type.isin(["receiving_yards", "rushing_yards"])
            & game_markets.timestamp.between(
                kickoff, kickoff + pd.Timedelta(hours=4)
            )
        ]
        game_pbp = pbp[pbp.game_id.eq(game_id)]
        players = game_markets[["player", "player_id", "team"]].drop_duplicates()
        states, timing = cumulative_states(game_pbp, players, kickoff)
        joined = join_markets(game_markets, states)
        joined["during_actual_game"] = joined.timestamp.between(
            timing["game_start"], timing["game_end"]
        )
        joined_games.append(joined)
        row = quality_row(game_id, joined, total_markets)
        quality.append(row)
        print(
            f"{game_id} | markets {row['markets']} ({row['post_markets']} post-active) | "
            f"players {row['players']} | "
            f"post {row['post_obs']:,} | joined {row['joined']:,} ({row['join_pct']:.1f}%) | "
            f"spacing {row['spacing_min']:.1f}m | play age {row['play_age_s']:.1f}s | "
            f"<=1m {row['within_1m_pct']:.1f}% | <=2m {row['within_2m_pct']:.1f}% | "
            f"spread {row['spread_c']:.1f}c | <=10c {row['spread_le10_pct']:.1f}% | "
            f"rec {row['receiving_rows']:,} | rush {row['rushing_rows']:,} | "
            f"pbp {timing['timed_plays']}/{timing['total_plays']} timed"
        )
        if joined.seconds_since_play.lt(0).any():
            print(f"  WARNING: {game_id} has negative matched-play ages")

    joined = pd.concat(joined_games, ignore_index=True).sort_values(
        ["game_id", "timestamp", "ticker"]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    joined.to_parquet(args.output, index=False)
    print(f"\nWrote {len(joined):,} rows for {joined.game_id.nunique()} games -> {args.output}")
    print("\nQUALITY SUMMARY")
    print(pd.DataFrame(quality).round(1).to_string(index=False))


if __name__ == "__main__":
    main()
