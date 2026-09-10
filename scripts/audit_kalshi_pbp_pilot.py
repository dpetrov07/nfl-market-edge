"""Audit timing, final stats, and quote quality for the eight-game pilot."""

from __future__ import annotations

import argparse
from pathlib import Path

import nflreadpy as nfl
import pandas as pd

from build_kalshi_pbp_pilot import (
    PILOT_GAMES,
    PLAYER_ID_OVERRIDES,
    PLAYER_TEAM_OVERRIDES,
    cumulative_states,
    join_markets,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kalshi",
        type=Path,
        default=Path("data/processed/kalshi_player_prop_history.parquet"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/processed/kalshi_pbp_pilot_audited.parquet"),
    )
    parser.add_argument("--pbp", type=Path)
    parser.add_argument("--player-stats", type=Path)
    return parser.parse_args()


def load_inputs(args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame]:
    if args.pbp:
        pbp = pd.read_parquet(args.pbp)
    else:
        loaded_pbp = nfl.load_pbp(2025)
        pbp = loaded_pbp.filter(
            loaded_pbp["game_id"].is_in(PILOT_GAMES)
        ).to_pandas()
    if args.player_stats:
        stats = pd.read_parquet(args.player_stats)
    else:
        loaded_stats = nfl.load_player_stats(2025, summary_level="week")
        stats = loaded_stats.filter(
            loaded_stats["game_id"].is_in(PILOT_GAMES)
        ).to_pandas()
    return pbp, stats


def add_quote_flags(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.sort_values(["market_id", "timestamp"]).copy()
    frame["missing_bid_or_ask"] = frame.yes_bid.isna() | frame.yes_ask.isna()
    frame["empty_book"] = frame.yes_bid.le(0) & frame.yes_ask.ge(1)
    frame["crossed_book"] = frame.yes_bid.gt(frame.yes_ask)
    frame["spread_10c_or_less"] = (
        ~frame.missing_bid_or_ask
        & ~frame.empty_book
        & ~frame.crossed_book
        & frame.spread.between(0, 0.10)
    )
    change = frame.groupby("market_id").midpoint.diff().abs()
    two_sided = ~frame.missing_bid_or_ask & ~frame.empty_book & ~frame.crossed_book
    previous_two_sided = two_sided.groupby(frame.market_id).shift().eq(True)
    frame["suspicious_jump"] = (
        change.ge(0.25) & two_sided & previous_two_sided
    )
    changed = change.gt(0) | change.isna()
    run = changed.groupby(frame.market_id).cumsum()
    run_start = frame.groupby([frame.market_id, run]).timestamp.transform("min")
    frame["unchanged_minutes"] = (
        frame.timestamp - run_start
    ).dt.total_seconds() / 60
    frame["long_unchanged"] = frame.unchanged_minutes.ge(10)
    frame["quote_usable"] = (
        frame.during_actual_game
        & frame.timing_safe.fillna(False)
        & frame.spread_10c_or_less
    )
    return frame


def final_stat_mismatches(
    game_id: str, states: pd.DataFrame, official: pd.DataFrame
) -> pd.DataFrame:
    final = (
        states.sort_values("state_available_at")
        .drop_duplicates("player_id", keep="last")
        [[
            "player", "player_id", "targets_so_far", "receptions_so_far",
            "receiving_yards_so_far", "carries_so_far", "rushing_yards_so_far",
        ]]
    )
    names = {
        "targets_so_far": "targets",
        "receptions_so_far": "receptions",
        "receiving_yards_so_far": "receiving_yards",
        "carries_so_far": "carries",
        "rushing_yards_so_far": "rushing_yards",
    }
    rows = []
    official = official[official.game_id.eq(game_id)].set_index("player_id")
    for row in final.itertuples(index=False):
        if row.player_id not in official.index:
            continue
        source = official.loc[row.player_id]
        for cumulative, stat in names.items():
            calculated = getattr(row, cumulative)
            expected = source[stat]
            if pd.isna(expected):
                expected = 0
            if calculated != expected:
                rows.append(
                    {
                        "game_id": game_id,
                        "player": row.player,
                        "stat": stat,
                        "calculated": calculated,
                        "official": expected,
                    }
                )
    return pd.DataFrame(rows)


def market_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.sort_values(["market_id", "timestamp"]).copy()
    frame["spacing_s"] = frame.groupby("market_id").timestamp.diff().dt.total_seconds()
    grouped = frame.groupby("market_id")
    return grouped.agg(
        game_id=("game_id", "first"),
        ticker=("ticker", "first"),
        player=("player", "first"),
        prop_type=("prop_type", "first"),
        threshold=("threshold", "first"),
        market_in_game_observations=("timestamp", "size"),
        market_median_spacing_s=("spacing_s", "median"),
        market_max_gap_s=("spacing_s", "max"),
        market_median_spread=("spread", "median"),
        market_spread_10c_pct=("spread_10c_or_less", "mean"),
        market_missing_quote_pct=("missing_bid_or_ask", "mean"),
        market_empty_book_pct=("empty_book", "mean"),
        market_suspicious_jumps=("suspicious_jump", "sum"),
        market_long_unchanged_pct=("long_unchanged", "mean"),
        market_usable_rows=("quote_usable", "sum"),
        market_usable_pct=("quote_usable", "mean"),
    ).reset_index()


def quote_text(row: pd.Series | None) -> str:
    if row is None:
        return "missing"
    def cents(value: object) -> str:
        return "--" if pd.isna(value) else f"{100 * float(value):.0f}"
    return f"{cents(row.yes_bid)}/{cents(row.yes_ask)} mid {cents(row.midpoint)}c @ {row.timestamp:%H:%M:%S}Z"


def event_examples(audited: pd.DataFrame, state_frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    candidates = []
    market_keys = audited[[
        "game_id", "player_id", "player", "prop_type", "threshold", "market_id"
    ]].drop_duplicates()
    for game_id, states in state_frames.items():
        for player_id, player_states in states.groupby("player_id"):
            player_states = player_states.sort_values("state_available_at").copy()
            for prop, cumulative in [
                ("receiving_yards", "receiving_yards_so_far"),
                ("rushing_yards", "rushing_yards_so_far"),
            ]:
                player_states["before"] = player_states[cumulative].shift().fillna(0)
                player_states["after"] = player_states[cumulative]
                changes = player_states[player_states.after.ne(player_states.before)]
                keys = market_keys[
                    market_keys.game_id.eq(game_id)
                    & market_keys.player_id.eq(player_id)
                    & market_keys.prop_type.eq(prop)
                ]
                if keys.empty:
                    continue
                for play in changes.itertuples(index=False):
                    crossing = keys[
                        keys.threshold.ge(play.before) & keys.threshold.lt(play.after)
                    ]
                    if not crossing.empty:
                        key = crossing.iloc[(crossing.threshold - play.after).abs().argmin()]
                        crossed = True
                    else:
                        key = keys.iloc[(keys.threshold - play.after).abs().argmin()]
                        crossed = False
                    quotes = audited[
                        audited.market_id.eq(key.market_id) & audited.during_actual_game
                    ].sort_values("timestamp")
                    before = quotes[quotes.timestamp.lt(play.play_start_timestamp)].tail(1)
                    after = quotes[quotes.timestamp.ge(play.state_available_at)].head(1)
                    later = quotes[
                        quotes.timestamp.ge(play.state_available_at + pd.Timedelta(minutes=1))
                        & quotes.timestamp.le(play.state_available_at + pd.Timedelta(minutes=2, seconds=30))
                    ].head(1)
                    if before.empty or after.empty or later.empty:
                        continue
                    b, a, l = before.iloc[0], after.iloc[0], later.iloc[0]
                    candidates.append(
                        {
                            "game_id": game_id,
                            "player": key.player,
                            "prop": prop.replace("_yards", ""),
                            "play_start": play.play_start_timestamp,
                            "state_available_at": play.state_available_at,
                            "before_yards": play.before,
                            "after_yards": play.after,
                            "threshold": key.threshold,
                            "crossed": crossed,
                            "before_quote": quote_text(b),
                            "first_safe_quote": quote_text(a),
                            "one_two_min_quote": quote_text(l),
                            "score": 1000 * crossed + 10 * abs(play.after - play.before)
                            - 100 * (a.spread if pd.notna(a.spread) else 1),
                        }
                    )
    examples = pd.DataFrame(candidates)
    if examples.empty:
        return examples
    # Favor threshold crossings, large plays, and coverage across games.
    examples = examples.sort_values("score", ascending=False)
    chosen = []
    seen_markets = set()
    per_game: dict[str, int] = {}
    for row in examples.itertuples(index=False):
        key = (row.game_id, row.player, row.prop, row.threshold)
        if key in seen_markets or per_game.get(row.game_id, 0) >= 2:
            continue
        chosen.append(row)
        seen_markets.add(key)
        per_game[row.game_id] = per_game.get(row.game_id, 0) + 1
        if len(chosen) == 15:
            break
    return pd.DataFrame(chosen).drop(columns="score")


def main() -> None:
    args = parse_args()
    pbp, official = load_inputs(args)
    markets = pd.read_parquet(args.kalshi, filters=[("game_id", "in", PILOT_GAMES)])
    markets["timestamp"] = pd.to_datetime(markets.timestamp, utc=True)
    markets["kickoff"] = pd.to_datetime(markets.kickoff, utc=True)
    markets["player_id"] = markets.player.map(PLAYER_ID_OVERRIDES).fillna(markets.player_id)
    markets["team"] = markets.team.fillna(markets.player.map(PLAYER_TEAM_OVERRIDES))
    missing_opponent = markets.opponent.isna() & markets.team.notna()
    markets.loc[missing_opponent, "opponent"] = markets.loc[missing_opponent].apply(
        lambda row: next(team for team in row.game_id.split("_")[2:] if team != row.team),
        axis=1,
    )

    joined_games = []
    state_frames = {}
    game_windows = {}
    mismatch_frames = []
    game_checks = {}

    print("RAW TIMING EXAMPLES")
    sample = pbp[pbp.end_clock_time.notna()].head(6)[
        ["game_id", "play_id", "qtr", "time", "time_of_day", "end_clock_time"]
    ]
    print(sample.to_string(index=False))
    starts = pd.to_datetime(pbp.time_of_day, utc=True, format="mixed", errors="coerce")
    ends = pd.to_datetime(pbp.end_clock_time, utc=True, format="mixed", errors="coerce")
    durations = (ends - starts).dt.total_seconds().dropna()
    print(
        f"Wall-clock duration: n={len(durations):,}, min={durations.min():.2f}s, "
        f"median={durations.median():.2f}s, max={durations.max():.2f}s, "
        f"negative={durations.lt(0).sum()}"
    )

    for game_id in PILOT_GAMES:
        game_markets = markets[markets.game_id.eq(game_id)].copy()
        kickoff = game_markets.kickoff.iloc[0]
        game_pbp = pbp[pbp.game_id.eq(game_id)].copy()
        players = game_markets[["player", "player_id", "team"]].drop_duplicates()
        states, timing = cumulative_states(game_pbp, players, kickoff)
        state_frames[game_id] = states
        game_windows[game_id] = timing
        active = game_markets[
            game_markets.prop_type.isin(["receiving_yards", "rushing_yards"])
            & game_markets.timestamp.between(kickoff, kickoff + pd.Timedelta(hours=4))
        ].copy()
        joined = join_markets(active, states)
        joined["during_actual_game"] = joined.timestamp.between(
            timing["game_start"], timing["game_end"]
        )
        joined_games.append(joined)
        mismatch_frames.append(final_stat_mismatches(game_id, states, official))

        count_decreases = 0
        yard_decreases = 0
        impossible_rec = 0
        for _, player_states in states.groupby("player_id"):
            ordered = player_states.sort_values("state_available_at")
            count_decreases += int(
                ordered[["targets_so_far", "receptions_so_far", "carries_so_far"]]
                .diff().lt(0).sum().sum()
            )
            yard_decreases += int(
                ordered[["receiving_yards_so_far", "rushing_yards_so_far"]]
                .diff().lt(0).sum().sum()
            )
            impossible_rec += int((ordered.receptions_so_far > ordered.targets_so_far).sum())
        score_decreases = int(
            game_pbp.sort_values(["order_sequence", "play_id"])[
                ["total_home_score", "total_away_score"]
            ].ffill().diff().lt(0).sum().sum()
        )
        game_checks[game_id] = {
            **timing,
            "count_decreases": count_decreases,
            "yard_decreases_from_negative_plays": yard_decreases,
            "receptions_gt_targets": impossible_rec,
            "score_decreases": score_decreases,
            "score_mismatch": int(
                timing["final_home_score"] != timing["official_home_score"]
                or timing["final_away_score"] != timing["official_away_score"]
            ),
            "official_stat_rows_missing": int(
                (~players.player_id.isin(
                    official.loc[official.game_id.eq(game_id), "player_id"]
                )).sum()
            ),
        }

    audited = add_quote_flags(pd.concat(joined_games, ignore_index=True))
    in_game = audited[audited.during_actual_game]
    metrics = market_metrics(in_game)
    metric_columns = [
        column for column in metrics
        if column.startswith("market_") and column != "market_id"
    ]
    audited = audited.merge(
        metrics[["market_id", *metric_columns]], on="market_id", how="left"
    )
    in_game = audited[audited.during_actual_game]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    audited.to_parquet(args.output, index=False)
    mismatches = pd.concat(mismatch_frames, ignore_index=True)

    old = pd.read_parquet("data/processed/kalshi_pbp_pilot.parquet", columns=["game_id", "timestamp", "market_id"])
    quality = []
    for game_id in PILOT_GAMES:
        frame = in_game[in_game.game_id.eq(game_id)]
        mm = metrics[metrics.game_id.eq(game_id)]
        check = game_checks[game_id]
        stat_mismatches = int(mismatches.game_id.eq(game_id).sum()) if not mismatches.empty else 0
        spread_le10 = 100 * frame.spread_10c_or_less.mean()
        median_spread = 100 * frame.spread.median()
        rating = "GOOD"
        if spread_le10 < 25 or median_spread > 20:
            rating = "POOR"
        elif (
            spread_le10 < 50
            or frame.empty_book.mean() > 0.05
            or stat_mismatches
            or check["score_mismatch"]
        ):
            rating = "USABLE WITH FILTERS"
        quality.append(
            {
                "game": game_id,
                "useful_rows": int(frame.quote_usable.sum()),
                "rows": len(frame),
                "stale_rows": int((~audited.loc[audited.game_id.eq(game_id), "during_actual_game"]).sum()),
                "markets": frame.market_id.nunique(),
                "players": frame.player.nunique(),
                "spacing_s": mm.market_median_spacing_s.median(),
                "max_gap_min": mm.market_max_gap_s.max() / 60,
                "spread_c": median_spread,
                "spread<=10%": spread_le10,
                "missing%": 100 * frame.missing_bid_or_ask.mean(),
                "empty%": 100 * frame.empty_book.mean(),
                "jumps>=25c": int(frame.suspicious_jump.sum()),
                "unchanged>=10m%": 100 * frame.long_unchanged.mean(),
                "timing_issues": int((frame.timestamp < frame.state_available_at).sum()),
                "stat_mismatches": stat_mismatches,
                "rating": rating,
            }
        )

    stale = int((~audited.during_actual_game).sum())
    print("\nGAME QUALITY")
    print(pd.DataFrame(quality).round(1).to_string(index=False))
    print("\nFINAL STAT MISMATCHES")
    print("none" if mismatches.empty else mismatches.to_string(index=False))
    print("\nSTATE / SCORE CHECKS")
    print(pd.DataFrame.from_dict(game_checks, orient="index")[[
        "count_decreases", "yard_decreases_from_negative_plays",
        "receptions_gt_targets", "score_decreases", "score_mismatch",
        "start_fallbacks", "terminal_fallbacks", "stat_play_fallbacks",
        "official_stat_rows_missing",
    ]].to_string())

    print("\nWORST MARKET-LEVEL QUOTE QUALITY (minimum 20 in-game rows)")
    bad = metrics[metrics.market_in_game_observations.ge(20)].sort_values(
        ["market_usable_pct", "market_median_spread"]
    ).head(15)
    print(bad[[
        "game_id", "player", "prop_type", "threshold", "market_in_game_observations",
        "market_median_spread", "market_spread_10c_pct", "market_empty_book_pct",
        "market_suspicious_jumps", "market_long_unchanged_pct", "market_usable_pct",
    ]].round(3).to_string(index=False))

    chi = markets[markets.game_id.eq("2025_09_CHI_CIN")]
    chi_live = set(old.loc[old.game_id.eq("2025_09_CHI_CIN"), "market_id"])
    no_live = chi[~chi.market_id.isin(chi_live)][["player", "prop_type", "threshold", "ticker"]].drop_duplicates()
    print("\nCHI/CIN MARKETS WITH NO POST-KICKOFF OBSERVATIONS")
    print(no_live.to_string(index=False))

    print("\nEVENT / QUOTE ALIGNMENT EXAMPLES (bid/ask and midpoint in cents)")
    examples = event_examples(audited, state_frames)
    print(examples.to_string(index=False))

    print(
        f"\nWrote {len(audited):,} flagged rows ({in_game.shape[0]:,} during the actual game; "
        f"{audited.quote_usable.sum():,} with "
        f"both sides and spread <=10c) across {audited.market_id.nunique()} markets -> {args.output}"
    )
    print(
        f"Flagged {stale:,} of the original {len(old):,} rows as pre-first-play or "
        f"post-final-play; strict usable survival = {100 * audited.quote_usable.sum() / len(old):.1f}%."
    )


if __name__ == "__main__":
    main()
