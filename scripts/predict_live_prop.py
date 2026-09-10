"""Run football-only fair-value inference for one live player prop."""

from __future__ import annotations

import argparse
from pathlib import Path

import joblib
import pandas as pd


def parse_price(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 100 if value > 1 else value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=Path("models/nfl_fair_value_v0.joblib"))
    parser.add_argument("--prop", choices=["receiving_yards", "rushing_yards"], required=True)
    parser.add_argument("--threshold", type=float, required=True)
    parser.add_argument("--yards", type=float, required=True)
    parser.add_argument("--game-seconds-remaining", type=float, required=True)
    parser.add_argument("--quarter", type=float, required=True)
    parser.add_argument("--score-differential", type=float, default=0, help="Player team score minus opponent score")
    parser.add_argument("--targets", type=float, default=0)
    parser.add_argument("--receptions", type=float, default=0)
    parser.add_argument("--carries", type=float, default=0)
    parser.add_argument("--target-share", type=float, default=0)
    parser.add_argument("--carry-share", type=float, default=0)
    parser.add_argument("--team-pass-attempts", type=float, default=0)
    parser.add_argument("--team-rush-attempts", type=float, default=0)
    parser.add_argument("--team-offensive-plays", type=float, default=0)
    parser.add_argument("--yes-bid", type=float, help="Decimal probability or cents")
    parser.add_argument("--yes-ask", type=float, help="Decimal probability or cents")
    parser.add_argument("--min-edge", type=float, default=0.08)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    bundle = joblib.load(args.model)
    values = {
        "prop_receiving": float(args.prop == "receiving_yards"),
        "threshold": args.threshold,
        "yards_so_far": args.yards,
        "yards_to_threshold": args.threshold - args.yards,
        "threshold_cleared": float(args.yards > args.threshold),
        "game_seconds_remaining": args.game_seconds_remaining,
        "quarter": args.quarter,
        "player_score_differential": args.score_differential,
        "targets_so_far": args.targets,
        "receptions_so_far": args.receptions,
        "carries_so_far": args.carries,
        "target_share_so_far": args.target_share,
        "carry_share_so_far": args.carry_share,
        "team_pass_attempts_so_far": args.team_pass_attempts,
        "team_rush_attempts_so_far": args.team_rush_attempts,
        "team_offensive_plays_so_far": args.team_offensive_plays,
    }
    x = pd.DataFrame([[values[name] for name in bundle["features"]]], columns=bundle["features"])
    fair_yes = (
        0.999
        if args.yards > args.threshold
        else float(bundle["model"].predict_proba(x)[0, 1])
    )
    fair_no = 1 - fair_yes

    print(f"Model: {bundle['model_name']}")
    print(f"Fair YES: {100 * fair_yes:.1f}%")
    print(f"Fair NO:  {100 * fair_no:.1f}%")

    yes_bid = parse_price(args.yes_bid)
    yes_ask = parse_price(args.yes_ask)
    if yes_bid is None or yes_ask is None:
        return
    no_ask = 1 - yes_bid
    yes_edge = fair_yes - yes_ask
    no_edge = fair_no - no_ask
    if yes_edge >= args.min_edge and yes_edge >= no_edge:
        label = "BUY YES CANDIDATE"
    elif no_edge >= args.min_edge:
        label = "BUY NO CANDIDATE"
    else:
        label = "NO EDGE"
    print(f"YES edge vs {100 * yes_ask:.0f}c ask: {100 * yes_edge:+.1f} points")
    print(f"NO edge vs {100 * no_ask:.0f}c ask:  {100 * no_edge:+.1f} points")
    print(label)


if __name__ == "__main__":
    main()
